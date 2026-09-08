import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { z } from "zod";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, copyFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const EXE_PATH = join(__dirname, "windowscreenshot.exe");

function runCapture(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(EXE_PATH, args);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || `windowscreenshot.exe exited with code ${code}`));
    });
    proc.on("error", reject);
  });
}

function buildServer(): McpServer {
const server = new McpServer({ name: "windows-window-screenshot", version: "1.0.0" });

server.registerTool(
  "capture_window",
  {
    title: "Capture a screenshot of a specific window",
    description:
      "Captures a single window's current content as a PNG, by handle (from windows-inspect's window_list/window_children) " +
      "— works even if the window is not the foreground/active window or is partially covered by other windows, since it " +
      "renders the window's own content (PrintWindow) rather than cropping a screen capture. Does not work on a minimized window. " +
      "Optional x/y/width/height crop a sub-rectangle out of the captured bitmap (in that bitmap's own pixel space — not " +
      "necessarily 1:1 with window_children's reported rect), and maxWidth downscales proportionally if the result is wider " +
      "than that — use both to cut image-token cost when only a small region matters.",
    inputSchema: {
      hwnd: z.string().describe('Window handle, e.g. "0x001A04F2".'),
      savePath: z.string().optional().describe("Absolute path to also save the PNG to disk."),
      x: z.number().int().optional().describe("Crop: left edge, in the captured bitmap's own pixel space. Requires y/width/height too."),
      y: z.number().int().optional().describe("Crop: top edge, in the captured bitmap's own pixel space. Requires x/width/height too."),
      width: z.number().int().positive().optional().describe("Crop: width in pixels. Requires x/y/height too."),
      height: z.number().int().positive().optional().describe("Crop: height in pixels. Requires x/y/width too."),
      maxWidth: z.number().int().positive().optional().describe("Downscale proportionally if the (post-crop) image is wider than this."),
    },
  },
  async ({ hwnd, savePath, x, y, width, height, maxWidth }) => {
    const tmpDir = await mkdtemp(join(tmpdir(), "mcp-window-screenshot-"));
    const tmpFile = join(tmpDir, "window.png");
    try {
      const captureArgs = ["--action", "capture", "--hwnd", hwnd, "--out", tmpFile];
      if (x !== undefined && y !== undefined && width !== undefined && height !== undefined) {
        captureArgs.push("--cropX", String(x), "--cropY", String(y), "--cropWidth", String(width), "--cropHeight", String(height));
      }
      if (maxWidth !== undefined) captureArgs.push("--maxWidth", String(maxWidth));
      const resolution = await runCapture(captureArgs);
      const buffer = await readFile(tmpFile);

      if (savePath) {
        await copyFile(tmpFile, savePath);
      }

      return {
        content: [
          { type: "text" as const, text: `Captured ${resolution}${savePath ? ` and saved to ${savePath}` : ""}` },
          { type: "image" as const, data: buffer.toString("base64"), mimeType: "image/png" },
        ],
      };
    } catch (err) {
      return { isError: true, content: [{ type: "text" as const, text: `Window capture failed: ${(err as Error).message}` }] };
    } finally {
      await rm(tmpDir, { recursive: true, force: true });
    }
  }
);

server.registerTool(
  "capture_window_burst",
  {
    title: "Capture a timed burst of screenshots of a window",
    description:
      "Captures `count` screenshots of a window at `intervalMs` millisecond spacing, all in one call — for when frames " +
      "need to be captured faster than separate tool calls could be issued. Frames are written to disk under `savePath` " +
      "(created if missing) as frame_0001.png, frame_0002.png, ... and NOT returned inline (a real burst as inline images " +
      "would be a large context cost) — read a specific frame back afterward if you need to look at it.",
    inputSchema: {
      hwnd: z.string().describe('Window handle, e.g. "0x001A04F2".'),
      count: z.number().int().positive().describe("Number of frames to capture."),
      intervalMs: z.number().int().positive().describe("Milliseconds between the start of each frame capture."),
      savePath: z.string().describe("Absolute path to a directory to save the frames into (created if it doesn't exist)."),
    },
  },
  async ({ hwnd, count, intervalMs, savePath }) => {
    try {
      await mkdir(savePath, { recursive: true });
      const summary = await runCapture([
        "--action", "burst",
        "--hwnd", hwnd,
        "--count", String(count),
        "--intervalMs", String(intervalMs),
        "--outDir", savePath,
      ]);
      return { content: [{ type: "text" as const, text: summary }] };
    } catch (err) {
      return { isError: true, content: [{ type: "text" as const, text: `Burst capture failed: ${(err as Error).message}` }] };
    }
  }
);

return server;
}

// --http-port <port> lets many query() instances (one per Caroline tab) share
// ONE running copy of this server instead of each spawning its own -- per
// explicit instruction (2026-09-06): stdio transport is strictly 1:1 (one
// parent, one child), so N tabs meant N independent copies of every such
// server, confirmed live as the actual cause of a 240+ node.exe process
// swarm accumulating over a day of restarts. Falls back to stdio when the
// flag is absent, for any caller (an interactive `claude` session's own
// project-scoped .mcp.json, for instance) that still expects to spawn its
// own copy.
const httpPortIdx = process.argv.indexOf("--http-port");
if (httpPortIdx >= 0) {
  const port = Number(process.argv[httpPortIdx + 1]);
  createHttpServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/mcp") {
      res.writeHead(404).end();
      return;
    }
    // A stateless transport (sessionIdGenerator: undefined) can only ever handle ONE
    // request -- reusing it, or the McpServer/Protocol it's connected to, across
    // requests throws ("Stateless transport cannot be reused across requests" /
    // "Already connected to a transport", both from the SDK itself). Confirmed live
    // (2026-09-06) as the actual cause of every one of Caroline's shared utility MCP
    // servers 500ing on every call past their very first, for hours, surviving app
    // restarts (a deterministic bug, not a stuck process). Fresh server+transport per
    // request, per the SDK's own stateless example
    // (examples/server/simpleStatelessStreamableHttp.js), fixes it.
    const server = buildServer();
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res);
    } catch (err) {
      console.error("[mcp] request handling failed:", err);
      if (!res.headersSent) res.writeHead(500).end();
    }
    res.on("close", () => {
      transport.close();
      server.close();
    });
  }).listen(port, "127.0.0.1", () => {
    console.error(`[mcp] listening on http://127.0.0.1:${port}/mcp`);
  });
} else {
  const server = buildServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

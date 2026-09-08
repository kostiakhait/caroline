import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { z } from "zod";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, copyFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const EXE_PATH = join(__dirname, "capture.exe");

function runCapture(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(EXE_PATH, args);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || `capture.exe exited with code ${code}`));
    });
    proc.on("error", reject);
  });
}

function buildServer(): McpServer {
const server = new McpServer({ name: "windows-screenshot", version: "1.0.0" });

server.registerTool(
  "take_screenshot",
  {
    title: "Take a screenshot",
    description:
      "Captures the Windows screen and returns it as a PNG image. By default captures the full virtual screen (all monitors combined); pass 'monitor' to capture a single monitor by its zero-based index. " +
      "Optional x/y/width/height crop a sub-rectangle out of the captured bitmap (in that bitmap's own pixel space), and maxWidth downscales proportionally if the result is wider than that — use both to cut " +
      "image-token cost when only a small region matters.",
    inputSchema: {
      monitor: z
        .number()
        .int()
        .min(0)
        .optional()
        .describe("Zero-based monitor index to capture. Omit to capture the full virtual screen (all monitors)."),
      savePath: z.string().optional().describe("Absolute path to also save the PNG to disk."),
      x: z.number().int().optional().describe("Crop: left edge, in the captured bitmap's own pixel space. Requires y/width/height too."),
      y: z.number().int().optional().describe("Crop: top edge, in the captured bitmap's own pixel space. Requires x/width/height too."),
      width: z.number().int().positive().optional().describe("Crop: width in pixels. Requires x/y/height too."),
      height: z.number().int().positive().optional().describe("Crop: height in pixels. Requires x/y/width too."),
      maxWidth: z.number().int().positive().optional().describe("Downscale proportionally if the (post-crop) image is wider than this."),
    },
  },
  async ({ monitor, savePath, x, y, width, height, maxWidth }) => {
    const tmpDir = await mkdtemp(join(tmpdir(), "mcp-screenshot-"));
    const tmpFile = join(tmpDir, "screenshot.png");
    try {
      const args = ["--out", tmpFile];
      if (monitor !== undefined) args.push("--monitor", String(monitor));
      if (x !== undefined && y !== undefined && width !== undefined && height !== undefined) {
        args.push("--cropX", String(x), "--cropY", String(y), "--cropWidth", String(width), "--cropHeight", String(height));
      }
      if (maxWidth !== undefined) args.push("--maxWidth", String(maxWidth));

      const resolution = await runCapture(args);
      const buffer = await readFile(tmpFile);

      if (savePath) {
        await copyFile(tmpFile, savePath);
      }

      return {
        content: [
          {
            type: "text" as const,
            text: `Captured ${resolution}${savePath ? ` and saved to ${savePath}` : ""}`,
          },
          {
            type: "image" as const,
            data: buffer.toString("base64"),
            mimeType: "image/png",
          },
        ],
      };
    } catch (err) {
      return {
        isError: true,
        content: [{ type: "text" as const, text: `Screenshot failed: ${(err as Error).message}` }],
      };
    } finally {
      await rm(tmpDir, { recursive: true, force: true });
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

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { z } from "zod";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const EXE_PATH = join(__dirname, "windowmouse.exe");

function runWindowMouse(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(EXE_PATH, args);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || `windowmouse.exe exited with code ${code}`));
    });
    proc.on("error", reject);
  });
}

function buildServer(): McpServer {
const server = new McpServer({ name: "windows-window-mouse", version: "1.0.0" });

server.registerTool(
  "click_window",
  {
    title: "Click inside a specific window without focusing it",
    description:
      "Clicks at client-area coordinates (x, y relative to the window's own top-left, not the screen) inside a " +
      "given window, by handle (from windows-inspect's window_list/window_children). Delivered as posted mouse " +
      "messages directly to that window — does NOT move the real mouse cursor, call SetForegroundWindow, or " +
      "otherwise steal focus from whatever the user is doing, and works even if the window is not currently active. " +
      "Works for standard Win32 controls; GPU-rendered custom controls (Chromium/Electron, games) may not respond " +
      "to posted clicks and need a real click via windows-mouse instead.",
    inputSchema: {
      hwnd: z.string().describe('Window handle, e.g. "0x001A04F2".'),
      x: z.number().int().describe("X coordinate relative to the window's client area."),
      y: z.number().int().describe("Y coordinate relative to the window's client area."),
      button: z.enum(["Left", "Right", "Middle"]).optional().describe('Mouse button. Defaults to "Left".'),
    },
  },
  async ({ hwnd, x, y, button }) => {
    try {
      const args = ["--hwnd", hwnd, "--x", String(x), "--y", String(y)];
      if (button) args.push("--button", button);
      await runWindowMouse(args);
      return { content: [{ type: "text" as const, text: `Clicked (${x}, ${y}) in window ${hwnd}.` }] };
    } catch (err) {
      return { isError: true, content: [{ type: "text" as const, text: `Click failed: ${(err as Error).message}` }] };
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

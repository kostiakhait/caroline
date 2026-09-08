import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { z } from "zod";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const EXE_PATH = join(__dirname, "mouse.exe");

function runMouse(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(EXE_PATH, args);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || `mouse.exe exited with code ${code}`));
    });
    proc.on("error", reject);
  });
}

function parsePosition(output: string): { x: number; y: number } {
  const [x, y] = output.trim().split(",").map(Number);
  return { x, y };
}

const buttonSchema = z.enum(["Left", "Right", "Middle"]).optional().describe("Mouse button. Defaults to Left.");

function buildServer(): McpServer {
const server = new McpServer({ name: "windows-mouse", version: "1.0.0" });

server.registerTool(
  "get_mouse_position",
  {
    title: "Get mouse position",
    description: "Returns the current cursor position in screen coordinates.",
    inputSchema: {},
  },
  async () => {
    const out = await runMouse(["--action", "Position"]);
    const { x, y } = parsePosition(out);
    return { content: [{ type: "text" as const, text: `${x},${y}` }] };
  }
);

server.registerTool(
  "move_mouse",
  {
    title: "Move mouse",
    description: "Moves the cursor to an absolute screen position.",
    inputSchema: {
      x: z.number().int().describe("Target X coordinate in screen pixels."),
      y: z.number().int().describe("Target Y coordinate in screen pixels."),
    },
  },
  async ({ x, y }) => {
    const out = await runMouse(["--action", "Move", "--x", String(x), "--y", String(y)]);
    const pos = parsePosition(out);
    return { content: [{ type: "text" as const, text: `Moved to ${pos.x},${pos.y}` }] };
  }
);

server.registerTool(
  "click_mouse",
  {
    title: "Click mouse",
    description: "Clicks a mouse button, optionally after moving to a position first. Use clicks:2 for a double-click.",
    inputSchema: {
      button: buttonSchema,
      x: z.number().int().optional().describe("Move here before clicking. Omit to click at the current position."),
      y: z.number().int().optional().describe("Move here before clicking. Omit to click at the current position."),
      clicks: z.number().int().min(1).optional().describe("Number of clicks to perform (2 = double-click). Defaults to 1."),
    },
  },
  async ({ button, x, y, clicks }) => {
    const args = ["--action", "Click", "--button", button ?? "Left", "--clicks", String(clicks ?? 1)];
    if (x !== undefined && y !== undefined) args.push("--x", String(x), "--y", String(y));
    const out = await runMouse(args);
    const pos = parsePosition(out);
    return { content: [{ type: "text" as const, text: `Clicked ${button ?? "Left"} at ${pos.x},${pos.y}` }] };
  }
);

server.registerTool(
  "mouse_button",
  {
    title: "Press or release mouse button",
    description: "Presses or releases a mouse button without releasing/pressing it again. Pair a 'down' with a later 'up' to drag.",
    inputSchema: {
      action: z.enum(["down", "up"]).describe("Whether to press or release the button."),
      button: buttonSchema,
      x: z.number().int().optional().describe("Move here first. Omit to act at the current position."),
      y: z.number().int().optional().describe("Move here first. Omit to act at the current position."),
    },
  },
  async ({ action, button, x, y }) => {
    const args = ["--action", action === "down" ? "Down" : "Up", "--button", button ?? "Left"];
    if (x !== undefined && y !== undefined) args.push("--x", String(x), "--y", String(y));
    const out = await runMouse(args);
    const pos = parsePosition(out);
    return { content: [{ type: "text" as const, text: `${action === "down" ? "Pressed" : "Released"} ${button ?? "Left"} at ${pos.x},${pos.y}` }] };
  }
);

server.registerTool(
  "scroll_mouse",
  {
    title: "Scroll mouse wheel",
    description: "Scrolls the mouse wheel. Positive delta scrolls up/forward, negative scrolls down/backward, in wheel-notch units.",
    inputSchema: {
      delta: z.number().int().describe("Number of wheel notches. Positive = up, negative = down."),
    },
  },
  async ({ delta }) => {
    const out = await runMouse(["--action", "Scroll", "--delta", String(delta)]);
    const pos = parsePosition(out);
    return { content: [{ type: "text" as const, text: `Scrolled ${delta} notch(es) at ${pos.x},${pos.y}` }] };
  }
);

return server;
}

// --http-port <port> lets many query() instances (one per Caroline tab) share
// ONE running copy of this server instead of each spawning its own -- per
// explicit instruction (2026-09-06): stdio transport is strictly 1:1 (one
// parent, one child), so N tabs meant N independent copies of every such
// server, confirmed live as the actual cause of a 240+ node.exe process
// swarm accumulating over a day of restarts -- there's only one real cursor
// regardless of how many processes claim to control it, so sharing one
// process introduces no new contention that didn't already exist at the OS
// level. Falls back to stdio when the flag is absent, for any caller (an
// interactive `claude` session's own project-scoped .mcp.json, for
// instance) that still expects to spawn its own copy.
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

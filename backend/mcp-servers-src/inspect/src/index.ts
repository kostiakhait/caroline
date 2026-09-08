import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { z } from "zod";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const EXE_PATH = join(__dirname, "inspect.exe");

function runInspect(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(EXE_PATH, args);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || `inspect.exe exited with code ${code}`));
    });
    proc.on("error", reject);
  });
}

function jsonResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function errorResult(err: unknown) {
  return { isError: true, content: [{ type: "text" as const, text: (err as Error).message }] };
}

const filterSchema = {
  titleFilter: z.string().optional().describe("Case-insensitive substring match against the window title."),
  classNameFilter: z.string().optional().describe("Case-insensitive substring match against the window class name."),
  pid: z.number().int().positive().optional().describe("Restrict to windows owned by this process ID."),
  includeInvisible: z.boolean().optional().describe("Include invisible windows too. Defaults to false (hidden helper windows are usually noise)."),
};

function buildFilterArgs(opts: { titleFilter?: string; classNameFilter?: string; pid?: number; includeInvisible?: boolean }): string[] {
  const args: string[] = [];
  if (opts.titleFilter !== undefined) args.push("--titleFilter", opts.titleFilter);
  if (opts.classNameFilter !== undefined) args.push("--classNameFilter", opts.classNameFilter);
  if (opts.pid !== undefined) args.push("--pid", String(opts.pid));
  if (opts.includeInvisible !== undefined) args.push("--includeInvisible", String(opts.includeInvisible));
  return args;
}

function buildServer(): McpServer {
const server = new McpServer({ name: "windows-inspect", version: "1.0.0" });

server.registerTool(
  "window_list",
  {
    title: "List top-level windows",
    description:
      "Enumerates top-level windows (like Spy++/WinSpy's window browser), returning handle, title, class name, " +
      "owning process, screen rect, and visible/enabled state for each. Use titleFilter/classNameFilter/pid to " +
      "narrow down a busy desktop. The returned hwnd (a hex string, e.g. \"0x001A04F2\") is what you pass to " +
      "window_children/window_info and to the windows-window-screenshot/-mouse/-keyboard servers.",
    inputSchema: filterSchema,
  },
  async (opts) => {
    try {
      const out = await runInspect(["--action", "list", ...buildFilterArgs(opts)]);
      return jsonResult(out);
    } catch (err) {
      return errorResult(err);
    }
  }
);

server.registerTool(
  "window_children",
  {
    title: "List a window's child controls",
    description:
      "Enumerates the direct and nested child windows/controls of a given window (e.g. the edit box inside a " +
      "dialog) via EnumChildWindows, in the same shape as window_list — this is how you find the specific " +
      "control's hwnd to target for a click or keystroke.",
    inputSchema: { hwnd: z.string().describe('Parent window handle, e.g. "0x001A04F2" (from window_list).'), ...filterSchema },
  },
  async ({ hwnd, ...opts }) => {
    try {
      const out = await runInspect(["--action", "children", "--hwnd", hwnd, ...buildFilterArgs(opts)]);
      return jsonResult(out);
    } catch (err) {
      return errorResult(err);
    }
  }
);

server.registerTool(
  "window_info",
  {
    title: "Get full info for one window",
    description: "Returns the full record (handle, title, class name, owning process, rect, client rect, visible/enabled, control id, parent handle) for a single window handle.",
    inputSchema: { hwnd: z.string().describe('Window handle, e.g. "0x001A04F2".') },
  },
  async ({ hwnd }) => {
    try {
      const out = await runInspect(["--action", "info", "--hwnd", hwnd]);
      return jsonResult(out);
    } catch (err) {
      return errorResult(err);
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

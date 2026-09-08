import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { z } from "zod";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { resolveVk } from "./keys.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const EXE_PATH = join(__dirname, "windowkeyboard.exe");

function runWindowKeyboard(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(EXE_PATH, args);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || `windowkeyboard.exe exited with code ${code}`));
    });
    proc.on("error", reject);
  });
}

function buildServer(): McpServer {
const server = new McpServer({ name: "windows-window-keyboard", version: "1.0.0" });

server.registerTool(
  "type_window",
  {
    title: "Type text into a specific window/control without focusing it",
    description:
      "Types text directly into a given window or control, by handle (from windows-inspect's window_list/window_children). " +
      "Delivered as posted WM_CHAR messages — does NOT call SetFocus or SendInput, so it never steals focus and works even " +
      "if the window is not currently active. Works for standard Win32 edit/static controls; GPU-rendered custom text " +
      "inputs (Chromium/Electron) may ignore posted characters and need windows-keyboard's real SendInput instead.",
    inputSchema: {
      hwnd: z.string().describe('Window or control handle, e.g. "0x001A04F2".'),
      text: z.string().describe("Text to type."),
      delayMs: z.number().int().min(0).optional().describe("Delay between characters in milliseconds. Defaults to 10."),
    },
  },
  async ({ hwnd, text, delayMs }) => {
    try {
      const args = ["--action", "text", "--hwnd", hwnd, "--text", text];
      if (delayMs !== undefined) args.push("--delayms", String(delayMs));
      await runWindowKeyboard(args);
      return { content: [{ type: "text" as const, text: `Typed ${text.length} character(s) into window ${hwnd}.` }] };
    } catch (err) {
      return { isError: true, content: [{ type: "text" as const, text: `type_window failed: ${(err as Error).message}` }] };
    }
  }
);

server.registerTool(
  "press_window_key",
  {
    title: "Press a named key (optionally with modifiers) in a specific window/control without focusing it",
    description:
      "Presses a single named key (e.g. \"Enter\", \"Tab\", \"a\") with optional modifier keys (e.g. [\"Ctrl\"]) directly " +
      "in a given window/control, by handle — posted WM_KEYDOWN/WM_KEYUP messages, no focus change. Background modifier-combo " +
      "fidelity isn't guaranteed against every app (some read live modifier key state rather than trusting posted messages); " +
      "reliable for plain keys and most standard-control shortcuts.",
    inputSchema: {
      hwnd: z.string().describe('Window or control handle, e.g. "0x001A04F2".'),
      key: z.string().describe('Key name, e.g. "Enter", "Tab", "Escape", "a", "F5".'),
      modifiers: z.array(z.string()).optional().describe('Modifier key names to hold, e.g. ["Ctrl", "Shift"].'),
    },
  },
  async ({ hwnd, key, modifiers }) => {
    try {
      const vk = resolveVk(key);
      const modVks = (modifiers ?? []).map(resolveVk);
      const args = ["--action", "key", "--hwnd", hwnd, "--vk", String(vk)];
      if (modVks.length > 0) args.push("--modifiers", modVks.join(","));
      await runWindowKeyboard(args);
      return { content: [{ type: "text" as const, text: `Pressed ${[...(modifiers ?? []), key].join("+")} in window ${hwnd}.` }] };
    } catch (err) {
      return { isError: true, content: [{ type: "text" as const, text: `press_window_key failed: ${(err as Error).message}` }] };
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

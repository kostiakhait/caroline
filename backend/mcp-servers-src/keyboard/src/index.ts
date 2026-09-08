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
const EXE_PATH = join(__dirname, "keyboard.exe");

function runKeyboard(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(EXE_PATH, args);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || `keyboard.exe exited with code ${code}`));
    });
    proc.on("error", reject);
  });
}

function buildServer(): McpServer {
const server = new McpServer({ name: "windows-keyboard", version: "1.0.0" });

server.registerTool(
  "type_text",
  {
    title: "Type text",
    description:
      "Types arbitrary Unicode text by injecting one keystroke per character, the same way DictateWin's dictation typing works. Newlines are sent as literal characters, which most text fields treat as Enter.",
    inputSchema: {
      text: z.string().describe("The text to type."),
      delayMs: z.number().int().min(0).optional().describe("Delay in milliseconds between characters. Defaults to 10."),
    },
  },
  async ({ text, delayMs }) => {
    await runKeyboard(["--action", "Type", "--text", text, "--delayms", String(delayMs ?? 10)]);
    return { content: [{ type: "text" as const, text: `Typed ${text.length} character(s)` }] };
  }
);

server.registerTool(
  "press_key",
  {
    title: "Press a key",
    description:
      "Presses a single key, optionally combined with modifiers held down for the duration (e.g. key:'c', modifiers:['Ctrl'] for Ctrl+C). Key names: single letters/digits, or named keys like Enter, Escape, Tab, Backspace, Space, Left/Right/Up/Down, Home, End, PageUp, PageDown, Insert, Delete, F1-F24, Ctrl, Shift, Alt, Win.",
    inputSchema: {
      key: z.string().describe("Key to press, e.g. 'a', 'Enter', 'F5'."),
      modifiers: z
        .array(z.enum(["Ctrl", "Shift", "Alt", "Win"]))
        .optional()
        .describe("Modifier keys to hold down while pressing the key."),
    },
  },
  async ({ key, modifiers }) => {
    const vk = resolveVk(key);
    const modVks = (modifiers ?? []).map(resolveVk);
    await runKeyboard(["--action", "Press", "--vk", String(vk), "--modifiers", modVks.join(",")]);
    const combo = [...(modifiers ?? []), key].join("+");
    return { content: [{ type: "text" as const, text: `Pressed ${combo}` }] };
  }
);

server.registerTool(
  "key_down",
  {
    title: "Press and hold a key",
    description: "Presses a key down without releasing it. Pair with key_up to release, e.g. for holding a movement key or building a custom modifier combo across calls.",
    inputSchema: {
      key: z.string().describe("Key to hold down."),
    },
  },
  async ({ key }) => {
    const vk = resolveVk(key);
    await runKeyboard(["--action", "Down", "--vk", String(vk)]);
    return { content: [{ type: "text" as const, text: `Holding ${key} down` }] };
  }
);

server.registerTool(
  "key_up",
  {
    title: "Release a held key",
    description: "Releases a key previously pressed with key_down.",
    inputSchema: {
      key: z.string().describe("Key to release."),
    },
  },
  async ({ key }) => {
    const vk = resolveVk(key);
    await runKeyboard(["--action", "Up", "--vk", String(vk)]);
    return { content: [{ type: "text" as const, text: `Released ${key}` }] };
  }
);

return server;
}

// --http-port <port> lets many query() instances (one per Caroline tab) share
// ONE running copy of this server instead of each spawning its own -- per
// explicit instruction (2026-09-06): stdio transport is strictly 1:1 (one
// parent, one child), so N tabs meant N independent copies of every such
// server, confirmed live as the actual cause of a 240+ node.exe process
// swarm accumulating over a day of restarts -- there's only one real
// keyboard focus regardless of how many processes claim to control it, so
// sharing one process introduces no new contention that didn't already
// exist at the OS level. Falls back to stdio when the flag is absent, for
// any caller (an interactive `claude` session's own project-scoped
// .mcp.json, for instance) that still expects to spawn its own copy.
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

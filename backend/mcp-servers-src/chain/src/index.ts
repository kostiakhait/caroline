import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { z } from "zod";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { resolveVk } from "./keys.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const EXE_PATH = join(__dirname, "chain.exe");

function runChain(stepsFile: string, outFile: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(EXE_PATH, ["--stepsFile", stepsFile, "--out", outFile]);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      // chain.exe exits 0 whenever it ran to completion, even if the chain itself reports
      // status "failed"/"paused" in its JSON - exit code is reserved for "the exe couldn't run
      // at all" (bad steps file, crash), matching window-screenshot's exit-code convention.
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || `chain.exe exited with code ${code}`));
    });
    proc.on("error", reject);
  });
}

// Button, for click/mouse_down/mouse_up/drag.
const ButtonEnum = z.enum(["Left", "Right", "Middle"]).optional();

const MoveStep = z.object({
  op: z.literal("move"),
  hwnd: z.string().optional().describe('Target window handle (e.g. "0x001A04F2") or "$name" bound by an earlier launch/wait_window step. Omit for a global cursor move.'),
  x: z.number().int(),
  y: z.number().int().describe("Screen-absolute if hwnd is omitted; window-client-relative if hwnd is given."),
  retries: z.number().int().min(0).optional(),
  retryDelayMs: z.number().int().min(0).optional(),
});

const ClickStep = z.object({
  op: z.literal("click"),
  hwnd: z.string().optional(),
  x: z.number().int(),
  y: z.number().int(),
  button: ButtonEnum,
  clicks: z.number().int().positive().optional(),
  retries: z.number().int().min(0).optional(),
  retryDelayMs: z.number().int().min(0).optional(),
});

const MouseDownUpStep = z.object({
  op: z.enum(["mouse_down", "mouse_up"]),
  hwnd: z.string().optional(),
  x: z.number().int(),
  y: z.number().int(),
  button: ButtonEnum,
  retries: z.number().int().min(0).optional(),
  retryDelayMs: z.number().int().min(0).optional(),
});

const DragStep = z.object({
  op: z.literal("drag"),
  hwnd: z.string().optional().describe('Target window handle or "$name". Omit for a global (screen-absolute, focus-stealing) drag.'),
  x1: z.number().int(),
  y1: z.number().int(),
  x2: z.number().int(),
  y2: z.number().int(),
  button: ButtonEnum,
  steps: z.number().int().positive().optional().describe("Number of intermediate move events (default ~10) - a single down-jump-up does not reliably register as a drag in either mode; real intermediate moves are required."),
});

const KeyStep = z.object({
  op: z.literal("key"),
  hwnd: z.string().optional(),
  key: z.string().describe('Named key, e.g. "Enter", "Tab", "a", "F5".'),
  modifiers: z.array(z.string()).optional().describe('e.g. ["Ctrl", "Shift"].'),
  retries: z.number().int().min(0).optional(),
  retryDelayMs: z.number().int().min(0).optional(),
});

const TypeStep = z.object({
  op: z.literal("type"),
  hwnd: z.string().optional(),
  text: z.string(),
  delayMs: z.number().int().min(0).optional(),
});

const ScrollStep = z.object({
  op: z.literal("scroll"),
  hwnd: z.string().optional().describe('Target window handle or "$name". Omit for a global (wherever the real cursor is) scroll.'),
  x: z.number().int().optional().describe("Client-relative point to scroll at, when hwnd is given. Defaults to (0,0) if omitted."),
  y: z.number().int().optional(),
  delta: z.number().int().describe("Wheel notches. Positive = up/forward, negative = down/backward."),
});

const SleepStep = z.object({
  op: z.literal("sleep"),
  ms: z.number().int().positive(),
});

const WaitWindowStep = z.object({
  op: z.literal("wait_window"),
  titleFilter: z.string().optional(),
  classNameFilter: z.string().optional(),
  pid: z.union([z.number().int(), z.string()]).optional().describe("Literal PID or \"$name\" bound by an earlier launch step."),
  timeoutMs: z.number().int().positive(),
  as: z.string().optional().describe("Bind the matched window's owning PID under this name for later $name references."),
});

const WaitPixelStep = z.object({
  op: z.literal("wait_pixel"),
  hwnd: z.string().optional().describe('Target window handle or "$name" - reads from a PrintWindow capture of that window\'s own content (works even if it\'s covered/background). x/y become client-relative. Omit to read the live screen instead.'),
  x: z.number().int(),
  y: z.number().int(),
  color: z.string().describe('Target color as "#RRGGBB" or "RRGGBB".'),
  tolerance: z.number().int().min(0).max(255).optional().describe("Per-channel tolerance, default 0 (exact match)."),
  timeoutMs: z.number().int().positive(),
});

const WaitIdleStep = z.object({
  op: z.literal("wait_idle"),
  hwnd: z.string().optional().describe("Watch this window. Omit to watch a screen region instead (requires x/y/width/height)."),
  x: z.number().int().optional(),
  y: z.number().int().optional(),
  width: z.number().int().positive().optional(),
  height: z.number().int().positive().optional(),
  stableMs: z.number().int().positive().describe("How long the captured content must stay unchanged to count as idle."),
  timeoutMs: z.number().int().positive(),
});

const LaunchStep = z.object({
  op: z.literal("launch"),
  path: z.string(),
  args: z.string().optional(),
  cwd: z.string().optional(),
  as: z.string().optional().describe("Bind the launched process's PID under this name for later $name references."),
});

const KillStep = z.object({
  op: z.literal("kill"),
  pid: z.union([z.number().int(), z.string()]).optional().describe("Literal PID or \"$name\". Omit if using imageName."),
  imageName: z.string().optional().describe("Process image name (e.g. \"notepad\"), used with all:true."),
  all: z.boolean().optional().describe("Kill every process matching imageName, not just one."),
  force: z.boolean().optional().describe("Kill the entire process tree."),
});

const RestartStep = z.object({
  op: z.literal("restart"),
  pid: z.union([z.number().int(), z.string()]).optional(),
  imageName: z.string().optional(),
  all: z.boolean().optional(),
  force: z.boolean().optional(),
  path: z.string(),
  args: z.string().optional(),
  cwd: z.string().optional(),
  as: z.string().optional(),
});

const CheckpointStep = z.object({
  op: z.literal("checkpoint"),
  hwnd: z.string().optional().describe("Screenshot this window at the checkpoint. Omit for a full-screen screenshot."),
});

const StepSchema = z.discriminatedUnion("op", [
  MoveStep,
  ClickStep,
  MouseDownUpStep,
  DragStep,
  KeyStep,
  TypeStep,
  ScrollStep,
  SleepStep,
  WaitWindowStep,
  WaitPixelStep,
  WaitIdleStep,
  LaunchStep,
  KillStep,
  RestartStep,
  CheckpointStep,
]);

type Step = z.infer<typeof StepSchema>;

// Resolves named keys to VK codes here (same place window-keyboard does it) so the native
// interpreter only ever deals with numeric VKs. Everything else passes through unchanged; hwnd/
// pid "$name" references are resolved natively, at the moment each step actually runs, since the
// bound process/window may not exist yet when this file is written.
function toWireStep(step: Step): Record<string, unknown> {
  if (step.op === "key") {
    const { key, modifiers, ...rest } = step;
    return { ...rest, vk: resolveVk(key), modifierVks: (modifiers ?? []).map(resolveVk) };
  }
  // pid is typed as number | string (literal PID vs "$name") for schema ergonomics, but the wire
  // format needs one consistent JSON type - always send it as a string, since C# deserializes a
  // single Step DTO shared across all opcodes and a field that flips JSON type per-instance is
  // awkward to model there.
  if ((step.op === "kill" || step.op === "restart" || step.op === "wait_window") && "pid" in step && step.pid !== undefined) {
    return { ...step, pid: String(step.pid) };
  }
  return { ...step };
}

interface ChainResult {
  status: "ok" | "failed" | "paused";
  completedSteps: number;
  failedAt: { index: number; op: string; reason: string } | null;
  elapsedMs: number;
  screenshot: string | null;
}

function buildServer(): McpServer {
const server = new McpServer({ name: "windows-chain", version: "1.0.0" });

server.registerTool(
  "run_chain",
  {
    title: "Run a linear sequence of input/wait/process steps in one call",
    description:
      "Executes a fixed, linear (no branching, no loops) sequence of GUI-automation steps natively in a single call, instead of one LLM round-trip per step. " +
      "Mouse/keyboard steps run window-scoped (posted messages, no focus theft) when a step's hwnd is given, or screen-absolute (real SendInput/SetCursorPos, focus-stealing) when it's omitted " +
      "- the latter is required for GPU-rendered UIs (e.g. UE5 Slate) that ignore posted messages entirely. " +
      "A launch/wait_window step's `as` name can be referenced later as \"$name\" in an hwnd or pid field. " +
      "Use a `checkpoint` step to pause deliberately (not an error) before a risky/irreversible action - it returns immediately with the steps completed so far and a screenshot; resume with a fresh run_chain call against the current live state. " +
      "Stops at the first step that fails (after its own retries, if any) and reports exactly which step and why.",
    inputSchema: {
      steps: z.array(StepSchema).min(1),
    },
  },
  async ({ steps }) => {
    const tmpDir = await mkdtemp(join(tmpdir(), "mcp-chain-"));
    const stepsFile = join(tmpDir, "steps.json");
    const outFile = join(tmpDir, "checkpoint.png");
    try {
      await writeFile(stepsFile, JSON.stringify(steps.map(toWireStep)), "utf-8");
      const stdout = await runChain(stepsFile, outFile);

      let result: ChainResult;
      try {
        result = JSON.parse(stdout);
      } catch {
        return { isError: true, content: [{ type: "text" as const, text: `chain.exe produced non-JSON output: ${stdout}` }] };
      }

      const summary = { ...result, screenshot: result.screenshot ? "(attached below)" : null };
      const content: Array<{ type: "text"; text: string } | { type: "image"; data: string; mimeType: string }> = [
        { type: "text" as const, text: JSON.stringify(summary, null, 2) },
      ];

      if (result.screenshot) {
        try {
          await stat(result.screenshot);
          const buffer = await readFile(result.screenshot);
          content.push({ type: "image" as const, data: buffer.toString("base64"), mimeType: "image/png" });
        } catch {
          // Screenshot path reported but unreadable - fall through with just the text summary.
        }
      }

      return { isError: result.status === "failed", content };
    } catch (err) {
      return { isError: true, content: [{ type: "text" as const, text: `run_chain failed: ${(err as Error).message}` }] };
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

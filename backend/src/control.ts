import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
// backend/dist/control.js -> backend/node_modules/... -- the bundled,
// platform-specific claude.exe that @anthropic-ai/claude-agent-sdk pulls in
// via npm install. Self-contained: no separate Claude Code install needed
// on the target machine.
const CLAUDE_EXE = join(
  __dirname, "..", "node_modules", "@anthropic-ai", "claude-agent-sdk-win32-x64", "claude.exe",
);

function run(args: string[], cwd: string): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    const proc = spawn(CLAUDE_EXE, args, { cwd });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("error", reject);
    proc.on("close", (code) => resolve({ code: code ?? -1, stdout, stderr }));
  });
}

export function authStatus(cwd: string) {
  return run(["auth", "status"], cwd);
}

/**
 * Starts the bundled CLI's own browser-based OAuth flow (`claude auth login
 * --claudeai`, the default subscription login). The CLI opens the system
 * browser itself; stdout/stderr are piped (not inherited) because Caroline's
 * backend runs headless with no console of its own -- the caller streams
 * these lines to the chat/settings UI so the user can see progress or a
 * fallback URL if the browser doesn't open automatically.
 */
export function spawnAuthLogin(cwd: string) {
  return spawn(CLAUDE_EXE, ["auth", "login", "--claudeai"], { cwd });
}

export function authLogout(cwd: string) {
  return run(["auth", "logout"], cwd);
}

export function mcpList(cwd: string) {
  return run(["mcp", "list"], cwd);
}

export function mcpAdd(cwd: string, name: string, command: string, args: string[], scope: "local" | "user" | "project" = "local") {
  return run(["mcp", "add", "--scope", scope, name, "--", command, ...args], cwd);
}

/** Registers an HTTP-transport MCP server (a URL, not a spawned command) --
 *  see sharedMcpServers.ts's own doc comment for why this exists: many
 *  query() instances can share ONE already-running server this way, instead
 *  of each one spawning its own stdio copy. */
export function mcpAddHttp(cwd: string, name: string, url: string, scope: "local" | "user" | "project" = "local") {
  return run(["mcp", "add", "--transport", "http", "--scope", scope, name, url], cwd);
}

export function mcpGet(cwd: string, name: string) {
  return run(["mcp", "get", name], cwd);
}

export function mcpRemove(cwd: string, name: string) {
  return run(["mcp", "remove", name], cwd);
}

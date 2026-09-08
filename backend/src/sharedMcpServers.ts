import { spawn } from "node:child_process";
import { existsSync } from "node:fs";

/**
 * Launches ONE long-lived, HTTP-mode instance of each of Caroline's own
 * bundled utility MCP servers (mouse, keyboard, notes, time, etc.) at
 * backend startup -- shared across every tab's own query() instance instead
 * of each one spawning its own stdio copy. Per explicit instruction
 * (2026-09-06): confirmed live that stdio's strict 1:1 transport meant N
 * open tabs multiplied into N independent copies of every such server,
 * accumulating into a 240+ node.exe process swarm over a day of restarts
 * (each restart's OLD copies sometimes failing to exit -- see
 * processReaper.ts -- compounding the problem further).
 *
 * These processes are plain (non-detached) children of this Node process,
 * so the Kill(entireProcessTree:true) the WPF shell already does on shutdown
 * (BackendProcess.cs) tears them down along with everything else -- no
 * separate cleanup needed here.
 *
 * No restart-on-crash yet: if one of these dies mid-session, every tab loses
 * that one tool until the whole app restarts -- logged loudly (below) so
 * it's at least diagnosable, but a real supervisor is a follow-up, not part
 * of this fix.
 */

export interface SharedServerSpec {
  name: string;
  port: number;
  scriptPath: string;
}

export function launchSharedMcpServers(specs: SharedServerSpec[]): void {
  for (const spec of specs) {
    if (!existsSync(spec.scriptPath)) {
      console.error(`[caroline] [shared-mcp] ${spec.name}: script not found at ${spec.scriptPath}, skipping`);
      continue;
    }
    const proc = spawn(process.execPath, [spec.scriptPath, "--http-port", String(spec.port)], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    proc.stdout?.on("data", (d) => console.error(`[caroline] [shared-mcp:${spec.name}] ${d.toString().trim()}`));
    proc.stderr?.on("data", (d) => console.error(`[caroline] [shared-mcp:${spec.name}] ${d.toString().trim()}`));
    proc.on("exit", (code) =>
      console.error(
        `[caroline] [shared-mcp:${spec.name}] exited unexpectedly with code ${code} -- every tab's tools depending on it will fail to connect until Caroline restarts`,
      ),
    );
    console.error(`[caroline] [shared-mcp] launched ${spec.name} on http://127.0.0.1:${spec.port}/mcp (pid=${proc.pid})`);
  }
}

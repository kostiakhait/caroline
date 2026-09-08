import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { isAbsolute } from "node:path";

/**
 * Launches (once, kept running for this backend process's whole lifetime) the
 * local edge-tts HTTP server (python-scripts/local_tts_server.py) that voice.ts's
 * synthesizeSpeech calls in preference to Camerlengo's ai:tts. Per explicit
 * instruction (2026-09-07): the point is cutting per-call latency by never paying
 * Python's own interpreter-startup cost per TTS call, not saving money -- the
 * Camerlengo path stays as the fallback whenever this is unavailable or fails
 * (missing Python/edge-tts on an install that predates this feature, a transient
 * failure, etc.) and a SquirrelWisdom session exists.
 *
 * A plain (non-detached) child of this Node process, same reasoning as
 * sharedMcpServers.ts's own utility servers: the WPF shell's Kill(entireProcessTree:
 * true) on shutdown tears it down along with everything else, no separate cleanup
 * needed here. No restart-on-crash -- if it dies mid-session, synthesizeSpeech's own
 * fallback to Camerlengo covers every call until the next full app restart, so a
 * supervisor isn't worth the complexity yet.
 */
export const LOCAL_TTS_PORT = 9414;

export function localTtsUrl(): string {
  return `http://127.0.0.1:${LOCAL_TTS_PORT}/tts`;
}

export function launchLocalTtsServer(pythonExe: string, scriptPath: string): void {
  // existsSync only means anything for an absolute path -- pythonExe can legitimately
  // be the bare command "python" (dev runs / an install predating CAROLINE_PYTHON_PATH),
  // which only PATH resolution (inside spawn itself) can validate. Either way, the
  // spawned process's own "error" event below catches a genuine ENOENT.
  if (isAbsolute(pythonExe) && !existsSync(pythonExe)) {
    console.error(`[caroline] [local-tts] python not found at ${pythonExe} -- local TTS unavailable, every call will fall back to Camerlengo`);
    return;
  }
  if (!existsSync(scriptPath)) {
    console.error(`[caroline] [local-tts] script not found at ${scriptPath} -- local TTS unavailable, every call will fall back to Camerlengo`);
    return;
  }
  const proc = spawn(pythonExe, [scriptPath, "--port", String(LOCAL_TTS_PORT)], {
    stdio: ["ignore", "pipe", "pipe"],
  });
  proc.stdout?.on("data", (d) => console.error(`[caroline] [local-tts] ${d.toString().trim()}`));
  proc.stderr?.on("data", (d) => console.error(`[caroline] [local-tts] ${d.toString().trim()}`));
  proc.on("error", (err) =>
    console.error(`[caroline] [local-tts] failed to start (${err.message}) -- local TTS unavailable, every call will fall back to Camerlengo`),
  );
  proc.on("exit", (code) =>
    console.error(`[caroline] [local-tts] exited unexpectedly with code ${code} -- every TTS call will fall back to Camerlengo until Caroline restarts`),
  );
  console.error(`[caroline] [local-tts] launched on ${localTtsUrl()} (pid=${proc.pid})`);
}

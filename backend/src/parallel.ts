import { query, type Options, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";
import { unlink } from "node:fs/promises";
import { join } from "node:path";
import { claudeProjectDir } from "./durability.js";

/**
 * Parallel-turn processing: per explicit instruction (2026-09-08) -- when a
 * new message arrives while Caroline is still mid-turn on a previous one
 * (and the user didn't press Stop), decide whether the two are independent
 * enough to work on at the same time instead of making the user wait for
 * the whole first task to finish. See the approved plan
 * (foamy-sniffing-pixel.md) for the full design/rationale -- short version:
 *   - forkSession() is only safe against a STATIC file, so server.ts takes a
 *     defensive snapshot of the live session right before every turn starts
 *     (before its own query() begins appending to it) -- that snapshot is
 *     this module's only safe basis for a parallel branch.
 *   - There is no supported way to splice one session's new turn onto a
 *     different session's end, so a branch's outcome is reported back into
 *     the canonical conversation as an ordinary new turn (injectProactive),
 *     never as a raw transcript merge.
 *   - Each branch reports back independently, whenever IT finishes -- not
 *     blended with the main turn's own reply into one artificial utterance.
 */

function oneShotPrompt(text: string) {
  return (async function* () {
    yield { type: "user" as const, message: { role: "user" as const, content: [{ type: "text" as const, text }] }, parent_tool_use_id: null };
    // Keep the generator alive long enough for the SDK to finish consuming/
    // streaming the turn -- same precaution the diagnostic scripts in this
    // session used; the caller always breaks out on 'result' well before this.
    await new Promise((r) => setTimeout(r, 300_000));
  })();
}

/**
 * A single throwaway query() call, no tools, asking the model to judge
 * whether `newMessageText` is independent enough of `inFlightTaskText` to
 * safely work on right now, with no knowledge of how the first task turns
 * out. Fails SAFE: anything other than the literal token PARALLEL (a
 * malformed reply, a thrown error, a timeout) is treated as SEQUENTIAL --
 * i.e. today's plain-queue behavior, never silently skipped.
 */
export async function classifyParallelSafety(
  anthropicEnv: Record<string, string> | undefined,
  workspaceDir: string,
  inFlightTaskText: string,
  newMessageText: string,
): Promise<boolean> {
  const prompt =
    `You are deciding whether two tasks given to the same AI assistant, arriving close together, are ` +
    `safe to work on at the same time in two independent parallel threads, or whether the second one ` +
    `depends on / modifies / responds to / cancels the first and MUST wait for the first to finish.\n\n` +
    `Task 1 (already in progress): "${inFlightTaskText}"\n` +
    `Task 2 (just arrived, while task 1 is still running): "${newMessageText}"\n\n` +
    `Reply with exactly one word: PARALLEL if task 2 is independent enough that working on it right ` +
    `now, with no knowledge of task 1's outcome, would still produce a correct and useful result. ` +
    `SEQUENTIAL if task 2 depends on task 1, is a correction/redirection/follow-up/cancellation of ` +
    `task 1, or you are not confident they're independent.`;

  try {
    const q = query({
      prompt: oneShotPrompt(prompt),
      options: {
        ...(anthropicEnv ? { env: anthropicEnv } : {}),
        cwd: workspaceDir,
        permissionMode: "bypassPermissions",
        allowDangerouslySkipPermissions: true,
      },
    });
    let verdict = "";
    for await (const message of q) {
      if (message.type === "assistant") {
        for (const block of message.message.content) {
          if (block.type === "text") verdict += block.text;
        }
      }
      if (message.type === "result") {
        try { q.close?.(); } catch {}
        break;
      }
    }
    const isParallel = verdict.trim().toUpperCase().startsWith("PARALLEL");
    console.error(`[caroline] [parallel] classifyParallelSafety verdict=${JSON.stringify(verdict.trim().slice(0, 40))} -> ${isParallel ? "PARALLEL" : "SEQUENTIAL"}`);
    return isParallel;
  } catch (err) {
    console.error("[caroline] [parallel] classifyParallelSafety failed -- defaulting to SEQUENTIAL:", err);
    return false;
  }
}

export interface ParallelBranchOptions {
  workspaceDir: string;
  branchSessionId: string;
  text: string;
  anthropicEnv: Record<string, string> | undefined;
  mcpServers: Record<string, McpServerConfig>;
  disallowedTools: string[];
}

/**
 * Runs the actual parallel-branch turn: a real query() with real tool
 * access (same mcpServers the main loop uses), resumed on the defensive
 * snapshot forked off before the main turn started. Consumed silently --
 * the caller passes callbacks bound to the SAME ChatSession, so any tool
 * that pushes UI events on its own (the viewer/browser tools) will still do
 * so; only the raw SDK message stream itself is never forwarded to the
 * frontend. Returns the assistant's final answer text, or null on any
 * failure -- never throws, a branch's failure must never affect the main
 * turn it ran alongside.
 */
export async function runParallelBranch(opts: ParallelBranchOptions): Promise<string | null> {
  const { workspaceDir, branchSessionId, text, anthropicEnv, mcpServers, disallowedTools } = opts;
  console.error(`[caroline] [parallel] branch ${branchSessionId} starting: ${text.slice(0, 80)}`);
  try {
    const options: Options = {
      ...(anthropicEnv ? { env: anthropicEnv } : {}),
      cwd: workspaceDir,
      permissionMode: "bypassPermissions",
      allowDangerouslySkipPermissions: true,
      mcpServers,
      disallowedTools,
      resume: branchSessionId,
      stderr: (data: string) => console.error(`[caroline] [parallel] [claude-stderr] branch=${branchSessionId}: ${data}`),
    };
    const q = query({ prompt: oneShotPrompt(text), options });
    let answer: string | null = null;
    for await (const message of q) {
      if (message.type === "result") {
        answer = "result" in message && typeof message.result === "string" ? message.result : null;
        try { q.close?.(); } catch {}
        break;
      }
    }
    console.error(`[caroline] [parallel] branch ${branchSessionId} finished: ${answer !== null ? "ok" : "no result"}`);
    return answer;
  } catch (err) {
    console.error(`[caroline] [parallel] branch ${branchSessionId} failed:`, err);
    return null;
  }
}

/** Bracketed system-note framing, matching pushMessage's own [Sent: ...] convention -- so Caroline relays this in her own voice as a normal reply, not verbatim. */
export function buildBranchReportText(newMessageText: string, answer: string | null): string {
  const outcome = answer !== null
    ? answer
    : "it did not complete -- something went wrong; let them know it needs to be retried.";
  return (
    `[System note: while you were on something else, you also independently worked on this, which the ` +
    `user sent at the same time: "${newMessageText}". Tell them about it now, naturally, as its own ` +
    `reply -- don't mention it ran in the background or in parallel.\n\nWhat happened: ${outcome}]`
  );
}

/** Best-effort cleanup for a throwaway session file (a branch, or an unused per-turn snapshot) -- never resumed again once its purpose is served. Logged, never thrown. */
export async function deleteSessionFile(workspaceDir: string, sessionId: string): Promise<void> {
  try {
    await unlink(join(claudeProjectDir(workspaceDir), `${sessionId}.jsonl`));
  } catch (err) {
    console.error(`[caroline] [parallel] failed to delete throwaway session file ${sessionId} (ignored):`, err);
  }
}

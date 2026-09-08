import { forkSession } from "@anthropic-ai/claude-agent-sdk";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { claudeProjectDir } from "./durability.js";

/**
 * Ages out old context from a tab's ever-growing resumed Claude Code session
 * without ever touching the live/original transcript. A session is never
 * restarted on its own -- resume: sessionId persists across full app
 * restarts (durability.ts) -- so its .jsonl transcript on disk only grows,
 * and every historical tool_use/tool_result (screenshots especially: one
 * full PNG per app_browser_screenshot call) gets resent as input on every
 * subsequent turn. Confirmed live: one such file reached 285MB.
 *
 * Approach (per the design agreed 2026-09-04): fork the live session (SDK's
 * forkSession() -- the only sanctioned way to get a *new* session file with
 * correctly remapped uuids/parentUuid chain; there is no SDK API to filter
 * an existing session's content in place), then hand-edit the resulting
 * COPY's .jsonl lines directly. Mutating the copy is safe -- nothing reads
 * it until the caller points resume: at it and restarts the query(). The
 * original stays untouched, so a stub note pointing back at it always
 * resolves to something real.
 */

const ONE_HOUR_MS = 60 * 60 * 1000;
const ONE_DAY_MS = 24 * ONE_HOUR_MS;

type ContentBlock = { type: string; [key: string]: unknown };

interface RawEntry {
  type?: string;
  timestamp?: string;
  message?: { role?: string; content?: string | ContentBlock[]; stop_reason?: string | null; [key: string]: unknown };
  [key: string]: unknown;
}

function stubNote(parentPath: string, detail: string): ContentBlock {
  return {
    type: "text",
    text: `[${detail}, старше порога -- не передано модели. Полное содержимое сохранено в файле: ${parentPath}. Открыть при необходимости.]`,
  };
}

/** Rewrites tool_result content blocks in place: image blocks -> stub, everything else left alone (handled by the caller for the >1h tool-call rule). */
function filterToolResultBlock(block: ContentBlock, parentPath: string): ContentBlock {
  if (block.type !== "tool_result" || !Array.isArray(block.content)) return block;
  const innerBlocks = block.content as ContentBlock[];
  const filteredInner = innerBlocks.map((inner) =>
    inner.type === "image" ? stubNote(parentPath, "скриншот") : inner,
  );
  return { ...block, content: filteredInner };
}

/** >1h rule: strip image bytes (rule 1) and non-image tool_use/tool_result payloads (rule 3), keep the tool name for readability. */
function ageOutToolContent(content: ContentBlock[], parentPath: string): ContentBlock[] {
  return content.map((block) => {
    if (block.type === "image") return stubNote(parentPath, "скриншот");
    if (block.type === "tool_result") return filterToolResultBlock(block, parentPath);
    if (block.type === "tool_use") {
      // Only shrink `input` -- NOT add any extra property to this block.
      // Confirmed live (2026-09-05): an ad-hoc `__compacted` field added here
      // previously poisoned every resume that touched a stubbed entry --
      // the Anthropic API rejects unrecognized fields on a replayed content
      // block, so the very next turn after a compaction died immediately
      // with "query() stream ended unexpectedly", repeatedly, until the
      // session's restart budget was exhausted and Caroline gave up
      // entirely. `type`/`id`/`name` must stay exactly as the API expects a
      // tool_use block to look; only `input` shrinks.
      return { ...block, input: {} };
    }
    return block;
  });
}

/** Exported for direct unit testing of the filter logic without touching forkSession/fs. */
export function processEntry(entry: RawEntry, nowMs: number, parentPath: string): RawEntry {
  if (entry.type !== "user" && entry.type !== "assistant") return entry;
  const content = entry.message?.content;
  if (!Array.isArray(content)) return entry;
  if (!entry.timestamp) return entry;
  const ageMs = nowMs - Date.parse(entry.timestamp);
  if (!Number.isFinite(ageMs)) return entry;

  if (ageMs > ONE_DAY_MS) {
    // Rule 2: whole turn collapses to one note. uuid/parentUuid untouched --
    // the chain stays walkable, only this entry's payload shrinks. An
    // assistant message's stop_reason must stay consistent with its
    // (now stubbed) content -- "tool_use" with no tool_use block present
    // is exactly the kind of mismatch that broke resume once already (see
    // ageOutToolContent's own comment on the __compacted incident).
    const message = { ...entry.message, content: [stubNote(parentPath, "часть диалога")] };
    if (entry.type === "assistant" && message.stop_reason === "tool_use") message.stop_reason = "end_turn";
    return { ...entry, message };
  }
  if (ageMs > ONE_HOUR_MS) {
    return { ...entry, message: { ...entry.message, content: ageOutToolContent(content, parentPath) } };
  }
  return entry;
}

export interface CompactionResult {
  newSessionId: string;
  compactedAt: number;
}

/**
 * Returns null if it's not yet due (lastCompactedAt within the last hour).
 * lastCompactedAt === null (nothing recorded yet, e.g. this process just
 * started) always runs immediately -- covers "also on every app startup".
 *
 * Never throws -- this is background, optional housekeeping (per explicit
 * instruction, 2026-09-05): any failure here must never take down or block
 * the live conversation. The caller (runCompaction in server.ts) still
 * wraps this in its own try/catch as a second layer, but every real failure
 * mode already known (a bad fork, a delete that fails) is handled inline
 * instead of being allowed to propagate.
 */
export async function compactSessionIfDue(
  workspaceDir: string,
  currentSessionId: string,
  lastCompactedAt: number | null,
): Promise<CompactionResult | null> {
  const now = Date.now();
  if (lastCompactedAt !== null && now - lastCompactedAt < ONE_HOUR_MS) return null;

  const parentPath = join(claudeProjectDir(workspaceDir), `${currentSessionId}.jsonl`);
  const forkStartedAt = Date.now();
  const { sessionId: newSessionId } = await forkSession(currentSessionId, { dir: workspaceDir });
  console.error(`[caroline] compaction: forkSession(${currentSessionId}) -> ${newSessionId} took ${Date.now() - forkStartedAt}ms`);

  const forkPath = join(claudeProjectDir(workspaceDir), `${newSessionId}.jsonl`);
  const raw = await readFile(forkPath, "utf-8");
  const lines = raw.split("\n").filter((line) => line.length > 0);

  // Confirmed live (2026-09-05): forkSession() never copies CLI-internal
  // bookkeeping entries (last-prompt, mode, ai-title, queue-operation,
  // file-history-snapshot/delta) into a fork -- by design, every single
  // time, for every session, regardless of size or health. Entry-by-entry
  // diffing a real fork against its source showed 100% of the actual
  // conversation (user/attachment/assistant/system) preserved exactly, with
  // only those bookkeeping types zeroed out. A previous version of this
  // function treated a missing last-prompt as proof of a broken fork and
  // backed off to a fresh session instead of ever using it -- confirmed
  // live that this fired on literally every compaction attempt, all night,
  // discarding perfectly good forks every time. Directly tested: a fork
  // missing last-prompt resumes and responds completely normally. There is
  // currently no known real corruption signal to check for here -- forking
  // and resuming just works.
  const rewritten = lines.map((line) => {
    try {
      const entry = JSON.parse(line) as RawEntry;
      return JSON.stringify(processEntry(entry, now, parentPath));
    } catch (err) {
      console.error(`[caroline] compaction: failed to parse/process a line in fork ${forkPath} (passing through untouched):`, err);
      return line; // malformed/unknown line shape -- pass through untouched rather than risk corrupting it
    }
  });
  await writeFile(forkPath, rewritten.join("\n") + "\n", "utf-8");

  return { newSessionId, compactedAt: now };
}

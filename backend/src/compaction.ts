import { forkSession } from "@anthropic-ai/claude-agent-sdk";
import { readFile, writeFile, stat } from "node:fs/promises";
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
export const ONE_DAY_MS = 24 * ONE_HOUR_MS;
/**
 * Per explicit instruction (2026-09-09): replaces the old ">1h" age gate for
 * images/tool payloads/text -- confirmed live that a real session's ACTUAL
 * (API-relevant, toolUseResult excluded) content in the 1h-1day window
 * totaled ~940KB, none of it touched by the old rule (it only ever stripped
 * image/tool_use/tool_result blocks, never plain text), which is squarely in
 * the same order of magnitude as a real ~200K-token context window -- a
 * genuine, non-synthetic contributor to "Prompt is too long" under sw-proxy
 * (whose own native auto-compact is disabled, both by an upstream Claude
 * Code bug for any non-first-party ANTHROPIC_BASE_URL and by our own
 * settings override -- see server.ts's own comment on that). A byte budget
 * tracks the actual constraint directly instead of guessing via a clock.
 * Lowered 100KB -> 50KB (2026-09-09, explicit instruction) once this budget
 * also became enforced every turn (dehydrate.ts's agePreviousTurnsInPlace),
 * not just hourly/reactively -- the hourly pass here is now a redundant
 * backstop, not the primary enforcement, so it can afford to be tighter.
 * Exported so dehydrate.ts's in-place, per-turn version shares the exact
 * same number instead of risking drift between two copies.
 */
export const RECENT_CONTENT_BUDGET_BYTES = 50 * 1024; // 50KB

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
    text: `[${detail}, вытеснено из недавнего контекста -- не передано модели. Полное содержимое сохранено в файле: ${parentPath}. Открыть при необходимости.]`,
  };
}

/** Only replaces a text block if the replacement is actually smaller -- no point shrinking a two-word reply into a longer stub note. */
function stubTextIfWorthwhile(text: string, parentPath: string, detail: string): ContentBlock | { type: "text"; text: string } {
  const stub = stubNote(parentPath, detail);
  if (Buffer.byteLength(text, "utf-8") <= Buffer.byteLength(stub.text as string, "utf-8")) return { type: "text", text };
  return stub;
}

/**
 * Rewrites a tool_result block's content in place: image blocks -> stub
 * (array-content case, e.g. a screenshot tool), plain text blocks inside
 * the array -> stub too (2026-09-09 fix, see below), or the whole string ->
 * stub (string-content case -- Read, Bash, Grep, and most other tools
 * return their result as a plain string, not an array of blocks).
 *
 * Bug fix (2026-09-08): only the array-content case was ever handled here --
 * confirmed live via direct reproduction that a large STRING tool_result
 * (a big file Read, a big command's stdout) survived completely untouched
 * by this rule, unlike everything else the >1h/>1day rules already age out,
 * and directly inflates real API token usage on every later turn until the
 * full >1day turn-collapse eventually caught it.
 *
 * Bug fix (2026-09-09): the array-content branch itself only ever stubbed
 * `image` inner blocks -- most MCP tools actually wrap their result as
 * `[{type:"text", text: "..."}]`, which is the STANDARD shape, not the
 * exception. Confirmed live: after both the string-content fix above and
 * the 100KB recent-content budget, ~748 entries in one real session's
 * "outside the budget" window were STILL untouched, all of them exactly
 * this shape -- a tool_result array containing one large text block, never
 * covered by any rule until now.
 */
function filterToolResultBlock(block: ContentBlock, parentPath: string): ContentBlock {
  if (block.type !== "tool_result") return block;
  if (Array.isArray(block.content)) {
    const innerBlocks = block.content as ContentBlock[];
    const filteredInner = innerBlocks.map((inner) => {
      if (inner.type === "image") return stubNote(parentPath, "скриншот");
      if (inner.type === "text" && typeof inner.text === "string") {
        return stubTextIfWorthwhile(inner.text, parentPath, "результат инструмента");
      }
      return inner;
    });
    return { ...block, content: filteredInner };
  }
  if (typeof block.content === "string") {
    return { ...block, content: stubNote(parentPath, "результат инструмента").text };
  }
  return block;
}

/**
 * Applied to any entry the caller's recent-content-budget pass determined is
 * NOT within the live 100KB window: strip image bytes, non-image tool_use/
 * tool_result payloads, AND (2026-09-09) plain text -- the gap that let a
 * real ~940KB of untouched text sit in a "protected" window indefinitely.
 * A text block only gets replaced if it's actually bigger than the stub
 * note itself would be -- no point shrinking a two-word reply into a longer
 * stub.
 */
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
    if (block.type === "text" && typeof block.text === "string") {
      return stubTextIfWorthwhile(block.text, parentPath, "текст");
    }
    return block;
  });
}

/**
 * Exported for direct unit testing of the filter logic without touching
 * forkSession/fs. `keepLive` -- per explicit instruction (2026-09-09) -- is
 * computed by the caller's backward pass over ALL entries (see
 * compactSessionIfDue): true means this entry falls within the most recent
 * RECENT_CONTENT_BUDGET_BYTES of real (<=1day-old) content and stays
 * untouched; false means it's outside that window and gets aged out via
 * ageOutToolContent, UNLESS it's also >1day old, in which case the whole
 * turn collapses instead (that rule is independent of the byte budget).
 */
export function processEntry(entry: RawEntry, nowMs: number, parentPath: string, keepLive: boolean): RawEntry {
  if (entry.type !== "user" && entry.type !== "assistant") return entry;
  const content = entry.message?.content;
  if (!Array.isArray(content)) return entry;
  if (!entry.timestamp) return entry;
  const ageMs = nowMs - Date.parse(entry.timestamp);
  if (!Number.isFinite(ageMs)) return entry;

  if (ageMs > ONE_DAY_MS) {
    // Whole turn collapses to one note, regardless of the byte budget --
    // uuid/parentUuid untouched so the chain stays walkable, only this
    // entry's payload shrinks. An assistant message's stop_reason must stay
    // consistent with its (now stubbed) content -- "tool_use" with no
    // tool_use block present is exactly the kind of mismatch that broke
    // resume once already (see ageOutToolContent's own comment on the
    // __compacted incident).
    const message = { ...entry.message, content: [stubNote(parentPath, "часть диалога")] };
    if (entry.type === "assistant" && message.stop_reason === "tool_use") message.stop_reason = "end_turn";
    return { ...entry, message };
  }
  if (!keepLive) {
    return { ...entry, message: { ...entry.message, content: ageOutToolContent(content, parentPath) } };
  }
  return entry;
}

export interface CompactionResult {
  newSessionId: string;
  compactedAt: number;
}

/**
 * Returns null if it's not yet due (lastCompactedAt within the last hour) --
 * unless `force` is set, which skips that recency check entirely. `force` is
 * for server.ts's urgent-compaction path (a "Prompt is too long" turn: the
 * live session is already too big to even load, so waiting for the hourly
 * schedule isn't an option -- see that call site's own doc comment).
 * lastCompactedAt === null (nothing recorded yet, e.g. this process just
 * started) always runs immediately -- covers "also on every app startup".
 *
 * Never throws -- this is background, optional housekeeping (per explicit
 * instruction, 2026-09-05): any failure here must never take down or block
 * the live conversation. The caller (runCompaction in server.ts) still
 * wraps this in its own try/catch as a second layer, but every real failure
 * mode already known (a bad fork, a delete that fails) is handled inline
 * instead of being allowed to propagate. Purely algorithmic throughout --
 * local file reads/rewrites only, no model/API call anywhere in this
 * function -- so it works exactly the same whether or not any chat source
 * currently has usable tokens (per explicit instruction, 2026-09-08): the
 * urgent case above is specifically for when tokens are the problem.
 */
export async function compactSessionIfDue(
  workspaceDir: string,
  currentSessionId: string,
  lastCompactedAt: number | null,
  force = false,
): Promise<CompactionResult | null> {
  const now = Date.now();
  if (!force && lastCompactedAt !== null && now - lastCompactedAt < ONE_HOUR_MS) return null;

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
  const parsed: (RawEntry | null)[] = lines.map((line) => {
    try {
      return JSON.parse(line) as RawEntry;
    } catch (err) {
      console.error(`[caroline] compaction: failed to parse a line in fork ${forkPath} (passing through untouched):`, err);
      return null;
    }
  });

  // Backward pass (newest -> oldest): keep the most recent
  // RECENT_CONTENT_BUDGET_BYTES of real (<=1day-old) content live. >1day
  // entries never consume the budget -- they're collapsed by the separate
  // age rule inside processEntry regardless of where they'd otherwise fall.
  const keepLive: boolean[] = new Array(parsed.length).fill(false);
  let budgetRemaining = RECENT_CONTENT_BUDGET_BYTES;
  for (let i = parsed.length - 1; i >= 0; i--) {
    const entry = parsed[i];
    if (!entry || (entry.type !== "user" && entry.type !== "assistant")) continue;
    if (!Array.isArray(entry.message?.content) || !entry.timestamp) continue;
    const ageMs = now - Date.parse(entry.timestamp);
    if (!Number.isFinite(ageMs) || ageMs > ONE_DAY_MS) continue;
    if (budgetRemaining <= 0) continue;
    keepLive[i] = true;
    budgetRemaining -= Buffer.byteLength(JSON.stringify(entry.message!.content), "utf-8");
  }

  const rewritten = lines.map((line, i) => {
    const entry = parsed[i];
    if (!entry) return line; // already logged above
    try {
      return JSON.stringify(processEntry(entry, now, parentPath, keepLive[i]));
    } catch (err) {
      console.error(`[caroline] compaction: failed to process a line in fork ${forkPath} (passing through untouched):`, err);
      return line;
    }
  });
  await writeFile(forkPath, rewritten.join("\n") + "\n", "utf-8");

  return { newSessionId, compactedAt: now };
}

/**
 * Plain filesystem stat -- no SDK call, no model call, nothing that can hang
 * or depend on any chat source having tokens. Per explicit instruction
 * (2026-09-08): urgent compaction must trigger on OUR OWN signal, not on
 * whatever the SDK does or doesn't say -- a session that's too big to even
 * reach 'init' may never produce a "Prompt is too long" message (or any
 * message) at all, confirmed live as a 326-SECOND silent stall with zero
 * SDK output before the stream just ended with nothing to detect. Checking
 * the transcript's own size on disk, before ever creating query(), catches
 * that case too. Returns null if the file doesn't exist yet (a session
 * that's never been resumed/persisted) -- not an error, just "nothing to
 * measure".
 */
export async function getSessionFileSizeBytes(workspaceDir: string, sessionId: string): Promise<number | null> {
  try {
    const filePath = join(claudeProjectDir(workspaceDir), `${sessionId}.jsonl`);
    const stats = await stat(filePath);
    return stats.size;
  } catch (err) {
    console.error(`[caroline] compaction: getSessionFileSizeBytes(${sessionId}) failed (treating as unknown/no file):`, err);
    return null;
  }
}

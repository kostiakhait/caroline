import { readFile, writeFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { claudeProjectDir } from "./durability.js";
import { RECENT_CONTENT_BUDGET_BYTES } from "./compaction.js";

/**
 * Per-explicit-instruction (2026-09-08): unlike compaction.ts's routine/urgent
 * passes (which only ever mutate a FORKED copy -- see that file's own doc
 * comment on why touching the live/original transcript is unsafe in general),
 * this rewrites the LIVE session file IN PLACE, every turn, replacing raw
 * image/document bytes with a link to a file already on disk. This is safe
 * ONLY because of WHEN the two call sites (server.ts) invoke it -- both are
 * points where no turn is in flight and the CLI subprocess is not currently
 * reading/writing the file:
 *   - runLoop's pre-resume check, before query() is even created (no live
 *     process yet at all this session lifetime).
 *   - inputStream()'s generator, awaited right before yielding the NEXT
 *     queued turn to the CLI -- the CLI only asks the generator for its next
 *     prompt once it has fully finished (and flushed to disk) the previous
 *     turn, so by construction there is no in-flight turn at that moment.
 * Never call this from anywhere else, and never let a call race a live
 * for-await loop still consuming messages for the CURRENT turn.
 */

type ContentBlock = { type: string; [key: string]: unknown };

interface RawEntry {
  type?: string;
  timestamp?: string;
  message?: { role?: string; content?: string | ContentBlock[]; [key: string]: unknown };
  [key: string]: unknown;
}

const DEHYDRATED_DIR_NAME = "dehydrated";

/** Exported so server.ts's expand-on-click handler can validate a requested path is actually inside this directory before reading it. */
export function dehydratedDir(workspaceDir: string): string {
  return join(workspaceDir, DEHYDRATED_DIR_NAME);
}

/** media_type "image/png" -> "png", "application/pdf" -> "pdf". Falls back to "bin" for anything unrecognized rather than failing the whole pass. */
function extensionFor(mediaType: unknown): string {
  if (typeof mediaType !== "string") return "bin";
  const subtype = mediaType.split("/")[1];
  return subtype ? subtype.split("+")[0] : "bin";
}

async function writeDehydratedFile(workspaceDir: string, mediaType: unknown, base64Data: string): Promise<string> {
  const dir = dehydratedDir(workspaceDir);
  if (!existsSync(dir)) await mkdir(dir, { recursive: true });
  const filePath = join(dir, `${randomUUID()}.${extensionFor(mediaType)}`);
  await writeFile(filePath, Buffer.from(base64Data, "base64"));
  return filePath;
}

/** Same idea as writeDehydratedFile, for plain text/JSON content (tool_result/text blocks) instead of base64 binary. */
async function writeDehydratedTextFile(workspaceDir: string, content: string): Promise<string> {
  const dir = dehydratedDir(workspaceDir);
  if (!existsSync(dir)) await mkdir(dir, { recursive: true });
  const filePath = join(dir, `${randomUUID()}.txt`);
  await writeFile(filePath, content, "utf-8");
  return filePath;
}

// Duplicated from server.ts's own formatTimestampForModel (kept in sync
// manually -- same convention as extractDehydratedFilePath's own doc comment
// below explains for the frontend copy of THAT function): dehydrate.ts can't
// import it directly without creating a server.ts <-> dehydrate.ts import
// cycle (server.ts already imports this file).
function formatTimestampForModel(d: Date): string {
  return d.toLocaleString("en-US", {
    weekday: "short", year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", timeZoneName: "short",
  });
}

/**
 * Per explicit instruction (2026-09-08): pushMessage's own "[Sent: ...]"
 * line (server.ts) only ever lands on a real/proactive USER turn -- every
 * OTHER entry (a tool_result-only user entry, every assistant entry) has no
 * timestamp at all once it reaches the model, even though the CLI itself
 * already records one on disk (entry.timestamp, never replayed to the model
 * -- only message.content is). Without this, Caroline can see "the user
 * just sent X" but has no way to tell how long ago HER OWN last reply, or a
 * slow tool call, actually happened -- confirmed live as a real gap, not
 * just theoretical. Stamped once per entry (not per block) here, in the
 * same per-turn pass that already touches every entry's content exactly
 * once (see dehydratePreviousTurns' alreadyThroughLine). Skipped for
 * entries that already start with a "[Sent: " block (a real/proactive user
 * turn) OR with this function's OWN stamp shape, so neither ever gets
 * double-stamped.
 *
 * Bug fix (2026-09-09): confirmed live -- this originally checked ONLY for
 * "[Sent: ", not its own stamp's shape. dehydratePreviousTurns' own
 * alreadyThroughLine skip normally makes that harmless (each entry visited
 * once, ever) -- but "tab X switching tracked session A -> B, rescanning
 * from line 0" (server.ts, whenever the tracked session id itself changes)
 * resets alreadyThroughLine to 0, and confirmed live that this can fire
 * repeatedly, in rapid succession, for the SAME two session ids oscillating
 * back and forth -- each rescan blindly prepended ANOTHER stamp, unbounded.
 * Found two real entries with 350+ duplicate stamp blocks each, almost
 * certainly what was then making those sessions fail/hang on resume. Now
 * recognized and skipped regardless of how many times a given entry gets
 * rescanned.
 */
const TIMESTAMP_STAMP_PATTERN = /^\[(Sent: |(Sun|Mon|Tue|Wed|Thu|Fri|Sat), )/;
function stampTimestampIfMissing(entry: RawEntry, content: ContentBlock[]): ContentBlock[] {
  const first = content[0];
  const alreadyStamped = first?.type === "text" && typeof first.text === "string" && TIMESTAMP_STAMP_PATTERN.test(first.text);
  if (alreadyStamped || typeof entry.timestamp !== "string") return content;
  const parsed = new Date(entry.timestamp);
  if (Number.isNaN(parsed.getTime())) return content;
  const stamp: ContentBlock = { type: "text", text: `[${formatTimestampForModel(parsed)}]` };
  return [stamp, ...content];
}

/** Same tone/shape as compaction.ts's stubNote() -- kept as a SEPARATE function (not shared)
 *  since the two mean different things: that one says "aged out, not resent"; this one says
 *  "already on disk from THIS same turn, and won't be resent from here on". */
function dehydratedNote(detail: string, filePath: string): ContentBlock {
  return {
    type: "text",
    text: `[${detail}, вытеснено на диск по завершении хода -- это НЕ прошлая сессия, это часть ТЕКУЩЕГО, ` +
      `непрерывающегося разговора, просто убранная из контекста для экономии места, и не передаётся повторно ` +
      `автоматически. Сохранено в файле: ${filePath}. Если содержимое сейчас нужно -- прочитай файл сам(а) ` +
      `через Read; не спрашивай пользователя, не проси прислать это заново и не называй это "прошлой сессией".]`,
  };
}

/**
 * Per explicit instruction (2026-09-09): a raw file path in prose isn't
 * useful to a HUMAN reading a recovered chat bubble (history.ts's get_history
 * fallback) -- nobody's going to go find and open the file by hand. This
 * lets the frontend detect one of OUR OWN notes (any of the templates in
 * this file -- "Сохранено"/"сохранена в файле: X." is the one substring
 * they all share, despite differing grammatical gender) and pull out the
 * exact path to offer as a clickable expand-in-place link instead. Single
 * source of truth for the pattern, so the write side (these templates) and
 * the read side (server.ts's expand_dehydrated_ref handler) can't drift.
 */
export function extractDehydratedFilePath(text: string): string | null {
  // Bug fix (2026-09-09): \w in a JS regex without the "u" flag is ASCII-only
  // (matches [A-Za-z0-9_]) -- it never matches Cyrillic letters at all, so
  // this silently matched nothing against real note text. Match the actual
  // gendered endings this file's templates use ("сохранено"/"сохранена")
  // explicitly instead.
  const m = text.match(/[Сс]охранен[оа] в файле: (.+?)\. /);
  return m ? m[1] : null;
}

/** Recurses into tool_result.content (screenshots and similar tool-produced images live there,
 *  never at the top level of a message). Returns the block unchanged (same reference) when there
 *  was nothing to dehydrate, so the caller can cheaply tell "nothing changed" without a deep-equal. */
async function dehydrateBlock(block: ContentBlock, workspaceDir: string): Promise<{ block: ContentBlock; changed: boolean }> {
  const source = (block as { source?: { type?: string; media_type?: unknown; data?: unknown } }).source;
  if (block.type === "image" && source?.type === "base64" && typeof source.data === "string") {
    const filePath = await writeDehydratedFile(workspaceDir, source.media_type, source.data);
    return { block: dehydratedNote("Изображение", filePath), changed: true };
  }
  if (block.type === "document" && source?.type === "base64" && typeof source.data === "string") {
    const filePath = await writeDehydratedFile(workspaceDir, source.media_type, source.data);
    return { block: dehydratedNote("Документ", filePath), changed: true };
  }
  const toolResultContent = (block as { content?: unknown }).content;
  if (block.type === "tool_result" && Array.isArray(toolResultContent)) {
    const innerBlocks = toolResultContent as ContentBlock[];
    let anyChanged = false;
    const newInner: ContentBlock[] = [];
    for (const inner of innerBlocks) {
      const result = await dehydrateBlock(inner, workspaceDir);
      if (result.changed) anyChanged = true;
      newInner.push(result.block);
    }
    if (!anyChanged) return { block, changed: false };
    return { block: { ...block, content: newInner }, changed: true };
  }
  return { block, changed: false };
}

/**
 * Per explicit instruction (2026-09-09): a `thinking` block is scratch work
 * for arriving at THAT turn's own answer -- once the turn is over, what
 * matters going forward is the outcome (text/tool_use/tool_result), not the
 * reasoning that produced it. The model doesn't need to re-read its own old
 * thinking to converse well later, so (unlike text/tool_result, which DO
 * carry real information worth keeping a reference to) this is dropped
 * outright, every turn, for every already-completed entry -- no extraction,
 * no stub note, nothing to point back at. Safe specifically because
 * dehydrateEntry/dehydratePreviousTurns only ever runs on entries from
 * ALREADY-COMPLETED turns (between-turn call sites only -- see this file's
 * own top doc comment), never on a turn still mid-generation, so there's no
 * live tool-use loop whose thinking block this could be pulling out from
 * under.
 */
async function dehydrateEntry(entry: RawEntry, workspaceDir: string): Promise<{ entry: RawEntry; changed: boolean }> {
  if (entry.type !== "user" && entry.type !== "assistant") return { entry, changed: false };
  const content = entry.message?.content;
  if (!Array.isArray(content)) return { entry, changed: false };
  let anyChanged = false;
  const newContent: ContentBlock[] = [];
  for (const block of content) {
    if (block.type === "thinking") {
      anyChanged = true;
      continue;
    }
    const result = await dehydrateBlock(block, workspaceDir);
    if (result.changed) anyChanged = true;
    newContent.push(result.block);
  }
  // Bug fix (2026-09-09): confirmed live that this is NOT a rare edge case --
  // a meaningful fraction of real assistant entries are ONE thinking block
  // and nothing else (the model's reasoning logged as its own transcript
  // line, separate from the text/tool_use that follows in a LATER entry).
  // The original version of this left those entries completely untouched
  // ("don't risk an empty content array"), which silently defeated the
  // whole point for exactly the entries most worth fixing. A single minimal
  // placeholder text block is unambiguously a valid, ordinary content shape
  // -- no guessing needed about whether the API accepts `content: []`.
  const finalContent = newContent.length > 0 ? newContent : [{ type: "text", text: "[мысли этого хода не сохраняются]" } as ContentBlock];
  const stampedContent = stampTimestampIfMissing(entry, finalContent);
  if (stampedContent !== finalContent) anyChanged = true;
  if (!anyChanged) return { entry, changed: false };
  return { entry: { ...entry, message: { ...entry.message, content: stampedContent } }, changed: true };
}

export interface DehydrationOutcome {
  changed: boolean;
  entriesChanged: number;
  linesRescanned: number;
  /** Pass this back in as `alreadyThroughLine` on the NEXT call for this same session id. */
  newThroughLine: number;
}

/**
 * Rewrites lines [alreadyThroughLine, EOF) of session `sessionId`'s live
 * .jsonl in place, replacing any raw image/document bytes with a link to a
 * file under workspace/dehydrated/. `alreadyThroughLine` lets the caller
 * skip re-parsing lines it already confirmed clean in an earlier call THIS
 * session lifetime -- the CLI only ever APPENDS to this file, never rewrites
 * past entries, so nothing before that line can newly need dehydrating.
 * Pass 0 for a session id this ChatSession hasn't dehydrated yet (a fresh
 * process lifetime, or the first call after a resume/fork changed which
 * session id is live).
 *
 * Never throws -- same "never take down the live conversation" guarantee as
 * compaction.ts: any failure here is logged and treated as "nothing
 * dehydrated this pass", not propagated.
 */
export async function dehydratePreviousTurns(
  workspaceDir: string,
  sessionId: string,
  alreadyThroughLine: number,
): Promise<DehydrationOutcome> {
  const filePath = join(claudeProjectDir(workspaceDir), `${sessionId}.jsonl`);
  let raw: string;
  try {
    raw = await readFile(filePath, "utf-8");
  } catch (err) {
    console.error(`[caroline] dehydrate: failed to read ${filePath} (skipping this pass):`, err);
    return { changed: false, entriesChanged: 0, linesRescanned: 0, newThroughLine: alreadyThroughLine };
  }
  const lines = raw.split("\n").filter((l) => l.length > 0);
  if (lines.length <= alreadyThroughLine) {
    return { changed: false, entriesChanged: 0, linesRescanned: 0, newThroughLine: lines.length };
  }

  let entriesChanged = 0;
  let anyChanged = false;
  const rewrittenTail: string[] = [];
  for (let i = alreadyThroughLine; i < lines.length; i++) {
    const line = lines[i];
    try {
      const entry = JSON.parse(line) as RawEntry;
      const result = await dehydrateEntry(entry, workspaceDir);
      if (result.changed) {
        anyChanged = true;
        entriesChanged++;
        rewrittenTail.push(JSON.stringify(result.entry));
      } else {
        rewrittenTail.push(line);
      }
    } catch (err) {
      console.error(`[caroline] dehydrate: failed to parse/process line ${i} of ${filePath} (passing through untouched):`, err);
      rewrittenTail.push(line);
    }
  }

  if (anyChanged) {
    const fullLines = [...lines.slice(0, alreadyThroughLine), ...rewrittenTail];
    await writeFile(filePath, fullLines.join("\n") + "\n", "utf-8");
    console.error(
      `[caroline] dehydrate: session ${sessionId} rewrote lines [${alreadyThroughLine}, ${lines.length}) in place, ` +
        `entriesChanged=${entriesChanged}`,
    );
  }

  return { changed: anyChanged, entriesChanged, linesRescanned: lines.length - alreadyThroughLine, newThroughLine: lines.length };
}

/**
 * Per explicit instruction (2026-09-09) -- this is the ONLY correct shape for
 * the recent-content budget, replacing two earlier wrong attempts (per-block,
 * then per-entry stubbing -- both left large amounts of small content
 * untouched "because a stub note wouldn't be any smaller", which defeats a
 * budget just as badly whether it happens block-by-block or entry-by-entry):
 *
 * Split the transcript in exactly TWO pieces. Walk backward from the newest
 * entry, accumulating real content bytes, until RECENT_CONTENT_BUDGET_BYTES
 * is reached -- that entry is the split point. Everything OLDER than the
 * split point (the whole prefix, as one contiguous chunk, in original
 * request/response order) gets written to ONE file. That entire prefix is
 * then removed from the live file and replaced by ONE reference entry --
 * placed where the prefix used to be, i.e. right before the live tail --
 * pointing at that one file. The live tail itself (the most recent ~budget
 * bytes) is never touched.
 *
 * The reference entry reuses the LAST removed entry's own identity (type,
 * uuid, timestamp, etc.) and only replaces its message.content -- this is
 * exactly why it works safely: the live tail's first entry's parentUuid
 * already equals that uuid (that's how the chain was built), so nothing
 * downstream needs to change at all, only this one entry's payload shrinks.
 * Same trick compaction.ts's own >1day rule already relies on.
 */
export interface AgeBudgetOutcome {
  changed: boolean;
  /** How many lines were collapsed into the one reference entry (0 if nothing was outside the budget). */
  linesCollapsed: number;
}

/** True if `entry` is itself an earlier call's reference entry -- lets a later split re-sweep it (and everything after it up to the new split point) into a fresh combined file without any special-casing. */
function isOwnReferenceEntry(entry: RawEntry): boolean {
  const content = entry.message?.content;
  return (
    Array.isArray(content) &&
    content.length === 1 &&
    content[0].type === "text" &&
    typeof content[0].text === "string" &&
    content[0].text.includes(DEHYDRATED_NOTE_MARKER)
  );
}

const DEHYDRATED_NOTE_MARKER = "вытеснено из истории по завершении хода";

function referenceNote(extractedPath: string): ContentBlock {
  return {
    type: "text",
    text: `[Более старая часть ЭТОГО ЖЕ, непрерывающегося разговора (НЕ прошлая сессия) ${DEHYDRATED_NOTE_MARKER} ` +
      `-- не передаётся повторно автоматически. Полностью сохранена в файле: ${extractedPath}. Если нужен более ` +
      `ранний контекст -- прочитай файл сам(а) через Read; не спрашивай пользователя, не проси прислать это ` +
      `заново и не называй это "прошлой сессией".]`,
  };
}

/**
 * In-place, per-turn equivalent of compaction.ts's recent-content byte
 * budget (see that module's own RECENT_CONTENT_BUDGET_BYTES doc comment for
 * why this exists -- the fork-based hourly/urgent version alone let real
 * content blow past the budget for up to an hour at a time). Call AFTER
 * dehydratePreviousTurns in the same pass -- images/documents/thinking
 * should already be gone from live entries by the time this runs.
 */
export async function agePreviousTurnsInPlace(workspaceDir: string, sessionId: string): Promise<AgeBudgetOutcome> {
  const filePath = join(claudeProjectDir(workspaceDir), `${sessionId}.jsonl`);
  let raw: string;
  try {
    raw = await readFile(filePath, "utf-8");
  } catch (err) {
    console.error(`[caroline] age-budget: failed to read ${filePath} (skipping this pass):`, err);
    return { changed: false, linesCollapsed: 0 };
  }
  const lines = raw.split("\n").filter((l) => l.length > 0);
  const parsed: (RawEntry | null)[] = lines.map((line) => {
    try {
      return JSON.parse(line) as RawEntry;
    } catch (err) {
      console.error(`[caroline] age-budget: failed to parse a line in ${filePath} (leaving the whole pass untouched):`, err);
      return null;
    }
  });
  if (parsed.some((e) => e === null)) {
    // Splitting requires a coherent view of the whole file (unlike
    // dehydrate's per-line rewrite) -- a single malformed line makes the
    // split point unreliable, so skip this pass entirely rather than risk
    // collapsing the wrong range. Will retry next turn.
    return { changed: false, linesCollapsed: 0 };
  }
  const entries = parsed as RawEntry[];

  // Walk backward, accumulating real content bytes, to find the split index
  // -- the first (oldest) entry that's still within budget. Everything
  // before it (0..splitIndex) is the prefix to collapse.
  let budgetRemaining = RECENT_CONTENT_BUDGET_BYTES;
  let splitIndex = 0;
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    const content = entry.message?.content;
    const isBudgetEligible = (entry.type === "user" || entry.type === "assistant") && Array.isArray(content);
    if (isBudgetEligible && budgetRemaining > 0) {
      budgetRemaining -= Buffer.byteLength(JSON.stringify(content), "utf-8");
    }
    if (budgetRemaining <= 0) {
      splitIndex = i;
      break;
    }
  }

  // Bug fix (2026-09-08): confirmed live -- a split landing between a
  // tool_use-ending assistant entry and its own tool_result reply (always
  // the PHYSICALLY NEXT entry: a tool call blocks the turn until the CLI
  // sends its result back) orphans that tool_result. The API rejects the
  // resumed session outright the moment it's next resumed ("API Error: 400
  // due to tool use concurrency issues.", confirmed live 2026-09-08) --
  // the tool_use entry ends up archived into the reference entry while its
  // own tool_result stays live with no matching tool_use anywhere left in
  // the file. Pull the tool_use entry (and, transitively, whatever's ahead
  // of it) into the live tail too whenever this would happen, so a pair
  // never gets split across the boundary. Self-limiting: the entry before a
  // tool_use-ending assistant entry is always a user entry (a real message
  // or an earlier tool_result), which never matches this condition itself.
  while (splitIndex > 0) {
    const boundary = entries[splitIndex - 1];
    const boundaryContent = boundary.message?.content;
    const nextContent = entries[splitIndex]?.message?.content;
    if (boundary.type !== "assistant" || !Array.isArray(boundaryContent) || !Array.isArray(nextContent)) break;
    const toolUseIds = new Set(
      boundaryContent.filter((b): b is ContentBlock & { id: string } => b.type === "tool_use" && typeof b.id === "string").map((b) => b.id),
    );
    if (toolUseIds.size === 0) break;
    const nextHasMatchingResult = nextContent.some((b) => b.type === "tool_result" && toolUseIds.has((b as { tool_use_id?: string }).tool_use_id ?? ""));
    if (!nextHasMatchingResult) break;
    splitIndex--;
  }

  // Nothing to collapse: either the whole transcript already fits the
  // budget (splitIndex never got set past 0), or the only thing before the
  // split point is our own existing reference entry from a previous pass
  // (re-wrapping a single reference entry into a new file gains nothing).
  const nothingToDo = splitIndex === 0 || (splitIndex === 1 && isOwnReferenceEntry(entries[0]));
  if (nothingToDo) {
    return { changed: false, linesCollapsed: 0 };
  }

  const prefixLines = lines.slice(0, splitIndex);
  const extractedPath = await writeDehydratedTextFile(workspaceDir, prefixLines.join("\n") + "\n");

  // Bug fix (2026-09-09): confirmed live against real data that the
  // conversation tree is NOT strictly linear -- 277 of 3559 entries in one
  // real session had a parentUuid that did NOT point at the immediately
  // preceding line (branches/retries sharing a common ancestor several
  // lines back). Reusing just entries[splitIndex-1]'s own uuid only fixes
  // the chain for whichever live-tail entry happens to point at THAT one
  // uuid -- any OTHER live-tail entry whose parent is a DIFFERENT uuid
  // somewhere in the collapsed range is left pointing at nothing. The only
  // correct fix: find every such dangling reference across the WHOLE live
  // tail and redirect all of them to the one new reference entry.
  const collapsedUuids = new Set(entries.slice(0, splitIndex).map((e) => e.uuid).filter((u): u is string => typeof u === "string"));

  const boundaryEntry = entries[splitIndex - 1];
  const referenceEntry: RawEntry = {
    ...boundaryEntry,
    // No longer meaningful to point further back into the now-extracted
    // prefix -- that whole chain is self-contained inside extractedPath.
    parentUuid: null,
    message: { ...boundaryEntry.message, content: [referenceNote(extractedPath)] },
  };
  if (referenceEntry.type === "assistant" && referenceEntry.message?.stop_reason === "tool_use") {
    referenceEntry.message.stop_reason = "end_turn";
  }

  let redirected = 0;
  const liveTail = entries.slice(splitIndex).map((entry, idx) => {
    if (typeof entry.parentUuid === "string" && collapsedUuids.has(entry.parentUuid)) {
      redirected++;
      return JSON.stringify({ ...entry, parentUuid: referenceEntry.uuid });
    }
    return lines[splitIndex + idx];
  });

  const newLines = [JSON.stringify(referenceEntry), ...liveTail];
  await writeFile(filePath, newLines.join("\n") + "\n", "utf-8");
  console.error(
    `[caroline] age-budget: session ${sessionId} collapsed ${splitIndex} old line(s) into ${extractedPath}, ` +
      `redirected ${redirected} dangling parentUuid reference(s), ${newLines.length} line(s) remain (was ${lines.length})`,
  );

  return { changed: true, linesCollapsed: splitIndex };
}

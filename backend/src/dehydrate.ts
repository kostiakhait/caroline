import { readFile, writeFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { claudeProjectDir } from "./durability.js";

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
  message?: { role?: string; content?: string | ContentBlock[]; [key: string]: unknown };
  [key: string]: unknown;
}

const DEHYDRATED_DIR_NAME = "dehydrated";

function dehydratedDir(workspaceDir: string): string {
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

/** Same tone/shape as compaction.ts's stubNote() -- kept as a SEPARATE function (not shared)
 *  since the two mean different things: that one says "aged out, not resent"; this one says
 *  "already on disk from THIS same turn, and won't be resent from here on". */
function dehydratedNote(detail: string, filePath: string): ContentBlock {
  return {
    type: "text",
    text: `[${detail}, вытеснено на диск по завершении хода -- не передаётся повторно модели. ` +
      `Сохранено в файле: ${filePath}. Прочитать через Read при необходимости.]`,
  };
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

async function dehydrateEntry(entry: RawEntry, workspaceDir: string): Promise<{ entry: RawEntry; changed: boolean }> {
  if (entry.type !== "user" && entry.type !== "assistant") return { entry, changed: false };
  const content = entry.message?.content;
  if (!Array.isArray(content)) return { entry, changed: false };
  let anyChanged = false;
  const newContent: ContentBlock[] = [];
  for (const block of content) {
    const result = await dehydrateBlock(block, workspaceDir);
    if (result.changed) anyChanged = true;
    newContent.push(result.block);
  }
  if (!anyChanged) return { entry, changed: false };
  return { entry: { ...entry, message: { ...entry.message, content: newContent } }, changed: true };
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

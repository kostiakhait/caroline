import { existsSync, readdirSync, statSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/**
 * Reads the real conversation history straight from the Claude Code CLI's
 * own session transcript (~/.claude/projects/<sanitized-workspace-path>/
 * *.jsonl) -- the authoritative record of everything said, independent of
 * the chat UI's own localStorage-based echo (see chat.js's TRANSCRIPT_KEY).
 *
 * Built as an emergency recovery path: switching chat.html/office_editor.html
 * from file:// to a virtual https://caroline.local origin (see MainWindow.
 * xaml.cs's SetVirtualHostNameToFolderMapping fix) silently wiped the
 * visible chat history, since localStorage is origin-scoped and the old
 * history lived under the old file: origin. The actual conversation was
 * never at risk -- Claude Code's own "continue" session persistence is
 * unrelated to browser storage -- but the user-visible transcript was, and
 * this is how it gets rebuilt: the chat UI calls "get_history" once on
 * startup if its own localStorage transcript is empty (see chat.js).
 */

export interface HistoryEntry {
  role: "user" | "assistant";
  text: string;
  ts: number;
  attachments?: { name: string }[];
}

function sanitizeProjectDirName(path: string): string {
  return path.replace(/[\\:]/g, "-");
}

// server.ts's attachmentToBlocks() sends an image/document/file as its own
// real content block (type "image"/"document", no filename anywhere on it)
// PLUS a paired text block noting where it was saved -- these three fixed
// prefixes are that note's only ones. Recognizing them here is how a
// recovered attachment gets its name back: the block itself never had one.
const ATTACHMENT_NOTE_PREFIXES = [
  "[This image is also saved at ",
  "[This document is also saved at ",
  "[Attached file saved to ",
];

/**
 * If `blockText` is one of attachmentToBlocks()'s own saved-path notes,
 * pulls the original filename back out of it (saveAttachmentToUploads names
 * files "<uuid>-<originalName>" -- stripping that prefix recovers the name
 * the user actually gave it). Returns null for ordinary message text.
 */
function extractAttachmentNote(blockText: string): { name: string } | null {
  for (const prefix of ATTACHMENT_NOTE_PREFIXES) {
    if (!blockText.startsWith(prefix)) continue;
    const rest = blockText.slice(prefix.length);
    const dashIdx = rest.indexOf(" -- ");
    const savedPath = (dashIdx >= 0 ? rest.slice(0, dashIdx) : rest.replace(/\.?\]\s*$/, "")).trim();
    const base = savedPath.split(/[\\/]/).pop() || savedPath;
    const name = base.replace(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-/i, "");
    return { name };
  }
  return null;
}

function latestSessionFile(workspaceDir: string): string | null {
  const projectDir = join(homedir(), ".claude", "projects", sanitizeProjectDirName(workspaceDir));
  if (!existsSync(projectDir)) return null;
  const files = readdirSync(projectDir).filter((f) => f.endsWith(".jsonl"));
  if (files.length === 0) return null;
  let best: string | null = null;
  let bestMtime = -Infinity;
  for (const f of files) {
    const full = join(projectDir, f);
    const mtime = statSync(full).mtimeMs;
    if (mtime > bestMtime) {
      bestMtime = mtime;
      best = full;
    }
  }
  return best;
}

// Bug fix (2026-09-09): this used to only look at type==="text" blocks and
// join them verbatim, which meant (a) an attached image/document was
// invisible here -- its own content block has no filename and isn't text at
// all, so it just vanished with no trace, not even a chip -- and (b) the
// paired "[This image is also saved at ...]" note (see attachmentToBlocks)
// leaked into the visible bubble text as ugly raw prose. Now the note is
// recognized, turned into a proper {name} the chat UI can offer to recover
// (see chat.js's renderAttachment/find_attachment), and excluded from `text`.
function extractTextAndAttachments(content: unknown): { text: string; attachments: { name: string }[] } {
  if (typeof content === "string") return { text: content, attachments: [] };
  if (!Array.isArray(content)) return { text: "", attachments: [] };
  const textParts: string[] = [];
  const attachments: { name: string }[] = [];
  for (const b of content as any[]) {
    if (b?.type !== "text" || typeof b.text !== "string") continue;
    const note = extractAttachmentNote(b.text);
    if (note) attachments.push(note);
    else textParts.push(b.text);
  }
  return { text: textParts.join("\n\n"), attachments };
}

/** Shared by readRecentHistory and readArchivedEntries -- both turn a raw JSONL blob into the same {role,text,ts,attachments?} shape the chat UI already knows how to render as bubbles. */
function extractEntriesFromJsonl(raw: string, sourceLabel: string): HistoryEntry[] {
  const entries: HistoryEntry[] = [];
  const lines = raw.split("\n");
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const obj = JSON.parse(line);
      if (obj.type !== "user" && obj.type !== "assistant") continue;
      const { text, attachments } = extractTextAndAttachments(obj.message?.content);
      if (!text.trim() && attachments.length === 0) continue;
      // Bug fix (2026-09-09): this rebuild path is entirely separate from
      // the live sdk_message stream's own [[NO_UPDATE]] suppression (see
      // chat.js's assistant-message handler) -- confirmed live, a
      // no-update turn's full text (explanation + trailing sentinel) was
      // leaking into the visible chat every time a tab reconnected and
      // replayed history via get_history, even after the live-path fix.
      // Checked as a substring, not exact equality, same reasoning as the
      // client-side fix: the model doesn't always reply with ONLY the
      // sentinel. Filtered here (server side, the single source both
      // readRecentHistory and readArchivedEntries draw from) rather than
      // only in chat.js's get_history handler, so a no-update turn never
      // even reaches the client as part of the user-visible transcript.
      if (obj.type === "assistant" && text.includes("[[NO_UPDATE]]")) continue;
      const ts = obj.timestamp ? Date.parse(obj.timestamp) : Date.now();
      entries.push({
        role: obj.type,
        text,
        ts: Number.isFinite(ts) ? ts : Date.now(),
        ...(attachments.length > 0 ? { attachments } : {}),
      });
    } catch (err) {
      console.error(`[caroline] readRecentHistory: skipping malformed line in ${sourceLabel}:`, err);
    }
  }
  return entries;
}

/**
 * Returns the most recent `limit` user/assistant text turns (tool-only
 * turns and non-text content are skipped, matching what the chat UI itself
 * ever rendered as a bubble). Best-effort: a corrupt or missing line is
 * skipped rather than failing the whole read.
 */
export function readRecentHistory(workspaceDir: string, limit = 200): HistoryEntry[] {
  const file = latestSessionFile(workspaceDir);
  if (!file) return [];
  return extractEntriesFromJsonl(readFileSync(file, "utf-8"), file).slice(-limit);
}

/**
 * Per explicit instruction (2026-09-09): a dehydration/archive note in a
 * recovered chat bubble is useless to a human unless they can actually see
 * what it's pointing at -- this turns one of dehydrate.ts's extracted
 * text-archive files (a whole collapsed prefix, dumped as raw JSONL) back
 * into the same {role,text,ts} shape readRecentHistory already produces, so
 * the frontend can render it exactly like ordinary recovered history when
 * the user clicks the link. Same non-text-content limitation as
 * readRecentHistory (images/tool calls are skipped, text only) -- this is
 * about a human reading it, not the model.
 */
export function readArchivedEntries(filePath: string): HistoryEntry[] {
  return extractEntriesFromJsonl(readFileSync(filePath, "utf-8"), filePath);
}

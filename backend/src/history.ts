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
}

function sanitizeProjectDirName(path: string): string {
  return path.replace(/[\\:]/g, "-");
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

function extractText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((b: any) => b?.type === "text" && typeof b.text === "string")
    .map((b: any) => b.text)
    .join("\n\n");
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

  const entries: HistoryEntry[] = [];
  const lines = readFileSync(file, "utf-8").split("\n");
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const obj = JSON.parse(line);
      if (obj.type !== "user" && obj.type !== "assistant") continue;
      const text = extractText(obj.message?.content);
      if (!text.trim()) continue;
      const ts = obj.timestamp ? Date.parse(obj.timestamp) : Date.now();
      entries.push({ role: obj.type, text, ts: Number.isFinite(ts) ? ts : Date.now() });
    } catch (err) {
      console.error(`[caroline] readRecentHistory: skipping malformed line in ${file}:`, err);
    }
  }
  return entries.slice(-limit);
}

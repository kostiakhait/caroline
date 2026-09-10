import { existsSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/**
 * Survives a FULL app restart (not just the in-process watchdog restart
 * handleFailure already replays from memory) -- if Caroline.exe gets closed
 * or crashes while a turn is still in flight, everything in the ChatSession
 * object is lost, but the user's message may already be sitting in the
 * conversation with no reply. Persisted here so the next process lifetime
 * can notice and proactively finish it instead of leaving it dangling until
 * the user happens to say something else.
 */
export interface PendingTurn {
  text: string;
  attachments: unknown[];
  submittedAtIso: string;
}

// Per tabId (see server.ts's multi-tab ChatSession map) -- a crash mid-turn
// on one tab must resume into that SAME tab on reconnect, not whichever tab
// happens to reconnect first. sanitizeTabId keeps a caller-supplied tabId
// (ultimately from a WS query string) from escaping the workspace dir.
function sanitizeTabId(tabId: string): string {
  return tabId.replace(/[^a-zA-Z0-9_-]/g, "_") || "default";
}

function pendingTurnPath(workspaceDir: string, tabId: string): string {
  return join(workspaceDir, `pending-turn-${sanitizeTabId(tabId)}.json`);
}

export function savePendingTurn(workspaceDir: string, tabId: string, text: string, attachments: unknown[]): void {
  const turn: PendingTurn = { text, attachments, submittedAtIso: new Date().toISOString() };
  try {
    writeFileSync(pendingTurnPath(workspaceDir, tabId), JSON.stringify(turn, null, 2) + "\n", "utf-8");
  } catch (err) {
    // Best-effort -- worst case this specific restart doesn't auto-resume --
    // but per explicit instruction (2026-09-06), that "worst case" must be
    // visible in the log, not just a silently swallowed possibility.
    console.error(`[caroline] savePendingTurn: write failed for tab ${tabId} (ignored):`, err);
  }
}

export function clearPendingTurn(workspaceDir: string, tabId: string): void {
  try {
    rmSync(pendingTurnPath(workspaceDir, tabId), { force: true });
  } catch (err) {
    console.error(`[caroline] clearPendingTurn: rmSync failed for tab ${tabId} (ignored):`, err);
  }
}

/**
 * Read-only -- does NOT delete the file. Root-caused live (2026-08-31): the
 * previous version deleted on read, called once at module load, well before
 * the resume message actually gets injected (that only happens once a
 * WebSocket client connects, see server.ts). During a run of quick
 * kill-and-restart cycles (confirmed live: the external health watchdog
 * killing a backend that hadn't finished starting yet, repeatedly), a
 * process could read-and-delete this file, then itself get killed before a
 * client ever connected to receive the injection -- silently and
 * permanently losing the user's unanswered message, with no trace anywhere.
 *
 * Deletion is no longer this function's job: the eventual resume message is
 * itself submit()'ed like any other turn, which calls savePendingTurn()
 * again (overwriting this file with the resume text) and clearPendingTurn()
 * normally once that turn actually completes -- the same safety net just
 * keeps covering the resume attempt itself if IT also gets interrupted,
 * rather than a one-shot "delete on read" leaving a asingle window where an
 * interruption loses everything with no recovery path at all.
 */
export function peekPendingTurn(workspaceDir: string, tabId: string): PendingTurn | null {
  const path = pendingTurnPath(workspaceDir, tabId);
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf-8")) as PendingTurn;
  } catch (err) {
    console.error(`[caroline] peekPendingTurn: read/parse failed for tab ${tabId} (treating as none):`, err);
    return null;
  }
}

// Matches Claude Code's own project-dir slugging: every ':' and path
// separator becomes '-', one-for-one (confirmed against a real project dir
// name, e.g. "C:\Users\x\...\workspace" -> "C--Users-x-...-workspace"). Used
// both here and by compaction.ts to locate a session's .jsonl transcript
// directly on disk.
export function claudeProjectDir(workspaceDir: string): string {
  const encoded = workspaceDir.replace(/[:\\/]/g, "-");
  return join(homedir(), ".claude", "projects", encoded);
}

// --- per-tab Claude session id, for resume: instead of continue: true ------
// continue:true always resumes "the most recent session for this cwd" --
// fine for a single conversation, but with multiple independent tabs
// sharing one workspace/cwd, every tab's continue:true would race to
// resume the SAME most-recent thread. Each tab instead gets resume:<its own
// stored session id>, captured from the SDK's own session_id field (present
// on every message) the first time each tab's query() starts one.
function tabSessionIdPath(workspaceDir: string, tabId: string): string {
  return join(workspaceDir, `tab-session-${sanitizeTabId(tabId)}.json`);
}

export function loadTabSessionId(workspaceDir: string, tabId: string): string | null {
  const path = tabSessionIdPath(workspaceDir, tabId);
  if (!existsSync(path)) return null;
  try {
    const data = JSON.parse(readFileSync(path, "utf-8")) as { sessionId?: string };
    return data.sessionId ?? null;
  } catch (err) {
    console.error(`[caroline] loadTabSessionId: read/parse failed for tab ${tabId} (treating as none):`, err);
    return null;
  }
}

export function saveTabSessionId(workspaceDir: string, tabId: string, sessionId: string): void {
  try {
    writeFileSync(tabSessionIdPath(workspaceDir, tabId), JSON.stringify({ sessionId }, null, 2) + "\n", "utf-8");
  } catch (err) {
    // Best-effort -- worst case this tab starts a fresh conversation next
    // time -- but per explicit instruction (2026-09-06), that must be
    // visible in the log, not just a silently swallowed possibility.
    console.error(`[caroline] saveTabSessionId: write failed for tab ${tabId} (ignored):`, err);
  }
}

/** Removes the stored resume id entirely -- the next runLoop iteration for
 *  this tab omits `resume` from its query() options, so the SDK starts a
 *  genuinely new session instead of resuming anything. Used by compaction's
 *  backoff path (see compaction.ts's backoffPointerNote) when forkSession
 *  itself cannot produce a usable copy of the current session no matter how
 *  many times it's retried. */
export function clearTabSessionId(workspaceDir: string, tabId: string): void {
  try {
    rmSync(tabSessionIdPath(workspaceDir, tabId), { force: true });
  } catch (err) {
    console.error(`[caroline] clearTabSessionId: rmSync failed for tab ${tabId} (ignored):`, err);
  }
}

// --- per-tab continuity-archive pointer ------------------------------------
// Bug fix (2026-09-09): confirmed live -- resetUnrecoverableSession's own
// archive-reference note only ever landed in the ONE turn it was attached
// to (a one-shot user-turn injection, not part of the system prompt). A
// later turn in the same fresh session has no such note in view at all, so
// Caroline had no way to know "this looks like the start of the
// conversation, but it isn't" -- confirmed live as a real incident (she
// flatly denied having just changed a mailbox password, because that whole
// exchange was several turns back in a session she genuinely has no other
// reason to reconsider). Persisted here (not just held in memory) so it
// survives a full app restart too, and read fresh into EVERY query()'s own
// systemPrompt (see server.ts's continuityPointerInstruction usage) for as
// long as it's set -- a real, standing instruction for the whole session's
// lifetime, not a single message that scrolls out of attention.
function tabContinuityArchivePath(workspaceDir: string, tabId: string): string {
  return join(workspaceDir, `tab-continuity-${sanitizeTabId(tabId)}.json`);
}

export function loadTabContinuityArchive(workspaceDir: string, tabId: string): string | null {
  const path = tabContinuityArchivePath(workspaceDir, tabId);
  if (!existsSync(path)) return null;
  try {
    const data = JSON.parse(readFileSync(path, "utf-8")) as { archivePath?: string };
    return data.archivePath ?? null;
  } catch (err) {
    console.error(`[caroline] loadTabContinuityArchive: read/parse failed for tab ${tabId} (treating as none):`, err);
    return null;
  }
}

export function saveTabContinuityArchive(workspaceDir: string, tabId: string, archivePath: string): void {
  try {
    writeFileSync(tabContinuityArchivePath(workspaceDir, tabId), JSON.stringify({ archivePath }, null, 2) + "\n", "utf-8");
  } catch (err) {
    console.error(`[caroline] saveTabContinuityArchive: write failed for tab ${tabId} (ignored):`, err);
  }
}

export function clearTabContinuityArchive(workspaceDir: string, tabId: string): void {
  try {
    rmSync(tabContinuityArchivePath(workspaceDir, tabId), { force: true });
  } catch (err) {
    console.error(`[caroline] clearTabContinuityArchive: rmSync failed for tab ${tabId} (ignored):`, err);
  }
}

// --- per-tab compaction pointer --------------------------------------------
// Same pattern/reasoning as the continuity-archive pointer above, but for
// routine age-based compaction (see compaction.ts) instead of an
// unrecoverable-session error -- see policies.ts's
// compactionPointerInstruction. Naturally overwritten on every subsequent
// compaction; no separate clear function needed.
function tabCompactionNotePath(workspaceDir: string, tabId: string): string {
  return join(workspaceDir, `tab-compaction-${sanitizeTabId(tabId)}.json`);
}

export function loadTabCompactionNote(workspaceDir: string, tabId: string): { parentPath: string | null; compactedAtIso: string | null } {
  const path = tabCompactionNotePath(workspaceDir, tabId);
  if (!existsSync(path)) return { parentPath: null, compactedAtIso: null };
  try {
    const data = JSON.parse(readFileSync(path, "utf-8")) as { parentPath?: string; compactedAtIso?: string };
    return { parentPath: data.parentPath ?? null, compactedAtIso: data.compactedAtIso ?? null };
  } catch (err) {
    console.error(`[caroline] loadTabCompactionNote: read/parse failed for tab ${tabId} (treating as none):`, err);
    return { parentPath: null, compactedAtIso: null };
  }
}

export function saveTabCompactionNote(workspaceDir: string, tabId: string, parentPath: string, compactedAtIso: string): void {
  try {
    writeFileSync(tabCompactionNotePath(workspaceDir, tabId), JSON.stringify({ parentPath, compactedAtIso }, null, 2) + "\n", "utf-8");
  } catch (err) {
    console.error(`[caroline] saveTabCompactionNote: write failed for tab ${tabId} (ignored):`, err);
  }
}

/**
 * One-time migration for users upgrading from pre-multi-tab Caroline: before
 * this, the single conversation was resumed via continue:true (whichever
 * session was most recently active for this cwd), not a stored id -- so the
 * primary tab (server.ts's PRIMARY_TAB_ID) would otherwise start a BLANK
 * conversation on first launch after the upgrade, silently dropping
 * everything discussed before. Best-effort: reads Claude Code's own session
 * transcript directory for this workspace directly off disk (~/.claude/
 * projects/<encoded-cwd>/*.jsonl) and returns the most recently modified
 * one's id, mirroring what continue:true would have picked. Never throws --
 * worst case (directory layout changes, permissions, anything) the caller
 * just falls through to starting fresh, same as any other new tab.
 */
export function findMostRecentClaudeSessionId(workspaceDir: string): string | null {
  try {
    const projectDir = claudeProjectDir(workspaceDir);
    if (!existsSync(projectDir)) return null;
    let best: { id: string; mtimeMs: number } | null = null;
    for (const entry of readdirSync(projectDir)) {
      if (!entry.endsWith(".jsonl")) continue;
      const mtimeMs = statSync(join(projectDir, entry)).mtimeMs;
      if (!best || mtimeMs > best.mtimeMs) best = { id: entry.slice(0, -".jsonl".length), mtimeMs };
    }
    return best?.id ?? null;
  } catch (err) {
    console.error(`[caroline] findMostRecentClaudeSessionId: failed for ${workspaceDir} (falling through to a fresh session):`, err);
    return null;
  }
}

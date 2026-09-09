import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, writeFileSync, statSync } from "node:fs";
import { join, resolve, extname, sep } from "node:path";
import { readFile as readFileAsync } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { WebSocketServer, type WebSocket } from "ws";
import { query, forkSession, type Options, type Query, type SDKMessage, type SDKUserMessage, type SDKRateLimitInfo, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";
import type { ContentBlockParam } from "@anthropic-ai/sdk/resources/messages/messages";
import { ensureWorkspace } from "./workspace.js";
import { authStatus, authLogout, spawnAuthLogin, mcpList, mcpAdd, mcpRemove } from "./control.js";
import {
  getPersona, getPersonaEditState, setProfileKey, saveCustomPersona, saveProfileOverride, resetProfile,
  personaSystemPromptAppend, type Persona,
} from "./persona.js";
import { transcribeAudio, synthesizeSpeech, voiceForGender, detectLanguage, cleanTextForSpeech } from "./voice.js";
import { createSchedulerTool, startDueCheckLoop, ensureRecurringBackup, type Reminder } from "./scheduler.js";
import { createFileOpenerTool, openFileWithDefaultApp } from "./files.js";
import { createViewerTool, takeViewerRequest, type OfficeConfig } from "./viewer.js";
import { finishOfficeEditSession } from "./officeEditor.js";
import { createLoginTool, takeLoginRequest, verifyAndSaveLogin, registerAndSaveLogin, isLoggedIn, getV2Session, openLoginRequest, loggedInEmail, clearCredentials } from "./login.js";
import { requireSwOrPrompt } from "./swGate.js";
import { resolveMode, buildOptionsEnv, getSwStatus, getOwnAnthropicApiKey, setOwnAnthropicApiKey, createTopupCheckoutUrl, markOwnAnthropicExhausted, clearOwnAnthropicExhausted, type ChatSource } from "./subscriptionMode.js";
import { getSmsAccountStatus, setSmsAccount, removeSmsAccount } from "./smsAccount.js";
import { isVisualModeEnabled, setVisualModeEnabled, resolveVisualModel } from "./visualMode.js";

/**
 * Best-effort v2 session for billing an ai:tts/ai:stt call against the
 * user's SquirrelWisdom wallet (see voice.ts) -- undefined (not logged in,
 * or the session fetch itself failed) just means that particular call goes
 * through unbilled/scope-gated-only, same as before billing existed here.
 */
async function swSessionOrUndefined(): Promise<string | undefined> {
  if (!isLoggedIn()) return undefined;
  try {
    return await getV2Session();
  } catch (err) {
    console.error("[caroline] swSessionOrUndefined: getV2Session failed (call proceeds unbilled):", err);
    return undefined;
  }
}
import { createEmailTool } from "./email/index.js";
import { createAppBrowserTool } from "./appBrowser.js";
import { createRatatoskTools } from "./ratatoskTools.js";
import { createConsultTools } from "./consultTools.js";
import { startRatatoskOwnerChannel, startRatatoskPresenceHeartbeat, getRatatoskChannelStatus } from "./ratatoskChannel.js";
import { hasOwnRatatoskAccount, ownRatatoskEmail, ensureOwnRatatoskAccount, getOwnV2Session } from "./ratatoskOwnAccount.js";
import { findOrCreateDM, sendMessage } from "./ratatosk.js";
import { vaultSecurityInstruction, progressNarrationInstruction, bashBackgroundInstruction, timestampAwarenessInstruction, noAlarmingInternalRecoveryInstruction, noUpdateSentinelInstruction, embeddedBrowserInstruction, noFullFilesystemSearchInstruction, recurringTasksInstruction, preferWindowTargetedInputInstruction, tableSizeGuidanceInstruction, cheapImageDescriptionInstruction, readContentNotHeadersInstruction, preferCroppedScreenshotsInstruction, consultLargeModelInstruction, noRemoteFilesystemScansInstruction, taskDecompositionInstruction, scriptOrSubagentDelegationInstruction, markDiscussedEmailsReadInstruction, checkSentMailTooInstruction, closeWindowsAfterTaskInstruction, learnFromMistakesInstruction, configureIsolatedGitBash } from "./policies.js";

// First thing this process ever does, before anything else runs. Confirmed
// live (2026-09-05) as a real, costly gap: with no explicit version marker
// logged at startup, a running process's actual code could only be
// inferred indirectly (comparing the backend process's own start time
// against dist/ file mtimes) -- error-prone and exactly what caused a long
// stretch of "fixed" code to be tested against a still-running OLD process
// that never actually picked it up. This makes it impossible to wonder.
console.error(`[caroline] === PROCESS STARTING === pid=${process.pid} server.js mtime=${statSync(fileURLToPath(import.meta.url)).mtime.toISOString()} startedAt=${new Date().toISOString()} descendantProcesses=${await describeDescendantProcesses(process.pid)}`);

configureIsolatedGitBash();
import { savePendingTurn, clearPendingTurn, peekPendingTurn, loadTabSessionId, saveTabSessionId, findMostRecentClaudeSessionId } from "./durability.js";
import { compactSessionIfDue, getSessionFileSizeBytes } from "./compaction.js";
import { dehydratePreviousTurns, agePreviousTurnsInPlace, dehydratedDir } from "./dehydrate.js";
import { classifyParallelSafety, runParallelBranch, buildBranchReportText, deleteSessionFile } from "./parallel.js";
import { snapshotDirectChildPids, findNewPid, scheduleReapIfStale, describeDescendantProcesses } from "./processReaper.js";
import { readRecentHistory, readArchivedEntries } from "./history.js";

const workspaceDir = await ensureWorkspace();
const BACKUP_NUDGE = "Time for your periodic memory backup: if Notes is available, save your current persona/reminders/anything worth keeping into the \"Caroline:Vault\" folder now (see your system instructions). If Notes isn't available, do nothing.";
ensureRecurringBackup(workspaceDir, BACKUP_NUDGE);

const PORT = Number(process.env.CAROLINE_PORT ?? 8765);

// How long we'll wait, with a turn in flight and no new SDK message at all,
// before deciding the session (and whatever MCP server it's waiting on) has
// hung and needs to be torn down and restarted. Not a per-tool-call timeout
// (the SDK/MCP layer already enforces those) -- this is the outer safety net
// for "the whole session went silent and nothing else is coming".
// The tab reminders/shutdown-sync/HTTP-API (tab-agnostic callers) and the
// pre-multi-tab-upgrade migration (see ChatSession.runLoop's resume option)
// target -- fixed rather than "whichever tab is active" both because
// "active" has no single meaning with multiple concurrent tabs, and so
// reminders land somewhere predictable regardless of which tabs happen to
// be open at the moment one fires. Must match MainWindow's own primary tab
// id (its first/un-closeable tab).
const PRIMARY_TAB_ID = "1";
const HANG_TIMEOUT_MS = 90_000;
// Cold MCP startup (17 servers, several full Chromium instances) gets far
// more patience than a mid-conversation stall before checkHang treats it as
// stuck -- see hasSeenInit's doc comment. Matches the WPF splash's own
// SplashMaxWait ceiling so the two layers agree on how long "still
// connecting" is allowed to mean exactly that.
const STARTUP_TIMEOUT_MS = 5 * 60_000;
const WATCHDOG_INTERVAL_MS = 5_000;
// Cap on how much of any single content block gets written to caroline.log
// per line -- keeps a huge email/webpage dump from making the log
// unreadable, while still keeping enough to actually diagnose something.
const LOG_CONTENT_MAX_CHARS = 4000;

function truncateForLog(s: string): string {
  if (s.length <= LOG_CONTENT_MAX_CHARS) return s;
  return s.slice(0, LOG_CONTENT_MAX_CHARS) + `... [truncated, ${s.length} chars total]`;
}

/**
 * Full-fidelity record of every non-streaming SDK message's actual content,
 * written to caroline.log -- independent of Claude Code's own session
 * transcript (~/.claude/projects/.../*.jsonl). Confirmed live (2026-08-31):
 * that transcript is NOT a reliable record for after-the-fact investigation
 * -- a suspected prompt-injection block Caroline flagged and refused could
 * not be traced back to its source afterward, because auto-compaction had
 * already summarized-and-discarded the turns that carried it (957K of 969K
 * tokens dropped in one compaction), leaving only Caroline's own paraphrase
 * behind. This log is deliberately NOT gated by silentTurn/UI visibility --
 * the point is a durable record of what actually happened in the session,
 * regardless of what reached the chat bubble, and it is never compacted or
 * pruned (caroline.log only grows, same as every other log line here).
 * Streamed partial-message deltas (message.type === "stream_event", see
 * includePartialMessages) are skipped -- only complete messages are logged,
 * to keep this from flooding on every token.
 */
function logSdkMessage(message: SDKMessage): void {
  try {
    if (message.type === "system" && message.subtype === "init") {
      console.error(`[caroline] [transcript] system/init: mcp_servers=${JSON.stringify(message.mcp_servers)}`);
      return;
    }
    if (message.type === "assistant") {
      for (const block of message.message.content) {
        if (block.type === "text") {
          console.error(`[caroline] [transcript] assistant text: ${truncateForLog(block.text)}`);
        } else if (block.type === "tool_use") {
          console.error(`[caroline] [transcript] assistant tool_use: ${block.name} input=${truncateForLog(JSON.stringify(block.input))}`);
        } else if (block.type === "thinking") {
          console.error(`[caroline] [transcript] assistant thinking: ${truncateForLog(block.thinking ?? "")}`);
        }
      }
      return;
    }
    if (message.type === "user") {
      const content = message.message.content;
      if (typeof content === "string") {
        console.error(`[caroline] [transcript] user text: ${truncateForLog(content)}`);
      } else if (Array.isArray(content)) {
        for (const block of content) {
          if (block.type === "text") {
            console.error(`[caroline] [transcript] user text: ${truncateForLog(block.text)}`);
          } else if (block.type === "tool_result") {
            const raw = typeof block.content === "string" ? block.content : JSON.stringify(block.content);
            console.error(`[caroline] [transcript] tool_result (tool_use_id=${block.tool_use_id}): ${truncateForLog(raw)}`);
          }
        }
      }
      return;
    }
    if (message.type === "result") {
      const r = message as { subtype?: string; duration_ms?: number; num_turns?: number; total_cost_usd?: number };
      console.error(`[caroline] [transcript] result: subtype=${r.subtype} duration_ms=${r.duration_ms} num_turns=${r.num_turns} total_cost_usd=${r.total_cost_usd}`);
    }
  } catch (err) {
    console.error("[caroline] logSdkMessage threw:", err);
  }
}
// How long to give interrupt() to actually take effect before escalating to
// close() -- see checkHang()'s doc comment for why this exists at all.
const HANG_ESCALATION_GRACE_MS = 20_000;
// Threshold for "restarts are happening too fast" -- beyond this many within
// RESTART_WINDOW_MS, handleFailure starts backing off between attempts (see
// RESTART_BACKOFF_MS below) instead of restarting instantly every time. It
// never gives up entirely: per explicit instruction (2026-09-05), a blocking
// dialog is only ever warranted when the user has something real to fix
// (e.g. a depleted balance) -- a generic repeating stream death never is, so
// this just means "retry forever, slower, and say so in the status bar."
const MAX_RESTARTS_PER_WINDOW = 5;
const RESTART_WINDOW_MS = 10 * 60_000;
// Flat, not exponential -- per explicit instruction (2026-09-08): once over
// budget, just retry once a minute until it recovers, no escalating delay.
const RESTART_BACKOFF_MS = 60_000;
// How long an abandoned query()'s CLI process gets to exit on its own before
// processReaper force-kills its whole tree -- see that module's own doc
// comment for the confirmed live leak this covers. Long on purpose (per
// explicit instruction, 2026-09-06): never wide enough to be mistaken for a
// session that's merely slow to wind down.
const REAP_GRACE_MS = 5 * 60_000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
// Per-tab context compaction (see compaction.ts): checked hourly, plus once
// shortly after this tab's own session starts up (covers "also on every app
// launch" without racing resolveResumeSessionId's own migration/read).
const COMPACTION_CHECK_INTERVAL_MS = 60 * 60_000;
const COMPACTION_STARTUP_DELAY_MS = 60_000;

// Matches the Claude Agent SDK's own synthesized text for an Anthropic API
// content-classifier refusal mid-turn (confirmed live 2026-09-02 -- e.g.
// triggered by an innocuous "clear out spam email" request, "Details: [bio]").
// This is NOT a Caroline bug: the SDK still completes the turn normally (a
// real 'result' message follows), it just replaces the assistant's actual
// reply with this boilerplate error text. Empirically (confirmed live: the
// same request, resubmitted by hand in the SAME session with no restart,
// succeeded) it's a transient, request-scoped classifier false-positive, not
// a poisoned session -- despite the wording, no actual "new session" is
// needed, just resubmitting the same turn works. See runLoop's handling
// below (auto-retry once, then tell the user plainly if it recurs).
const CLASSIFIER_REFUSAL_PATTERN = /can't help with this\.\s*Start a new session to continue/i;

function extractClassifierRefusalCategory(text: string): string | null {
  const m = text.match(/Details:\s*\[([^\]]+)\]/i);
  return m ? m[1] : null;
}

// Matches the two known "out of money" shapes a chat turn can fail with:
//  - the SquirrelWisdom proxy's own 402 (Api2AnthropicProxy.py's _json_error(402,
//    "insufficient_balance", "SquirrelWisdom wallet balance is too low")) -- used
//    when subscriptionMode's chatSource is "sw-proxy".
//  - Anthropic's own direct-API wording ("Your credit balance is too low to
//    access the Anthropic API...") -- used when chatSource is "own-anthropic-oauth"
//    or "own-anthropic-key" and the USER's own Anthropic account is out of credits.
// Unlike CLASSIFIER_REFUSAL_PATTERN, retrying does nothing here (the balance is
// still 0), so this gets handled by handleBalanceExhausted instead of an
// auto-retry -- see its own doc comment.
const SW_BALANCE_ERROR_PATTERN = /insufficient_balance|wallet balance is too low/i;
const ANTHROPIC_BALANCE_ERROR_PATTERN = /credit balance is too low/i;

// Claude Code CLI's own subscription usage-cap message -- distinct from BOTH
// the SDK's structured billing_error field and the SW/Anthropic balance-text
// shapes above: this is a session/monthly cap on the CLI's own subscription,
// not a wallet or account balance, and it resets at a stated time rather than
// needing a top-up. Confirmed live (2026-09-03): this slipped through as
// ordinary assistant text, shown as a raw, ugly chat bubble the user
// explicitly does not want -- "cc_cli_limit_message" is the marker Claude
// Code's own CLI stamps into this exact message. The original pattern only
// matched the literal phrase "monthly spend limit" -- confirmed live
// (2026-09-04) as too narrow: a SEPARATE Claude Code CLI message, "You've hit
// your session limit · resets 4:30pm (America/New_York)", slipped through
// the exact same way because it says "session limit", not "monthly spend
// limit". Broadened to match "hit your ... limit" generally (session/
// monthly/weekly/usage/whatever wording the CLI uses next), not just the one
// specific phrasing seen so far.
const CC_CLI_LIMIT_PATTERN = /hit your .*\blimit\b|monthly spend limit|cc_cli_limit_message/i;

// Confirmed live (2026-09-08): the CLI's own automatic-compaction feature fails
// outright under an env-overridden ANTHROPIC_API_KEY/BASE_URL (every chatSource
// except own-anthropic-oauth) with "Not logged in -- Please run /login" -- it
// needs the real OAuth ("firstParty") path specifically, not just a valid
// credential (`claude auth status` confirms loggedIn:true throughout). When
// compaction can't then shrink an oversized resumed session, the turn fails
// with this instead of a real reply -- observed as the CLI's very first
// response after 'init' on a big resumed session, with no real processing in
// between (init alone took 71s once). Per explicit instruction: never let
// this reach the chat -- run Caroline's own algorithmic compaction (works
// with or without tokens, see compaction.ts) immediately instead of waiting
// for the hourly schedule, and replay the turn that hit it once the
// now-smaller session is back up.
const PROMPT_TOO_LONG_PATTERN = /^Prompt is too long\b/i;
// Standalone occurrences of the SAME underlying compaction-auth failure (no
// "Prompt is too long" prefix this time) -- per explicit instruction: just
// suppress it, no action needed (the account's real login state is fine;
// this is specifically the CLI's own internal compaction call complaining).
const NOT_LOGGED_IN_PATTERN = /Not logged in/i;
// The one confirmed-failing session on 2026-09-08 was 12.2MB (barely
// shrunk by routine hourly compaction's age-based rules, since most of its
// bloat was recent). Set a bit below that to catch it before the resume
// attempt, not after -- a starting point, not a precisely derived number;
// revisit if it turns out to trip too early/late in practice.
const URGENT_COMPACTION_SIZE_THRESHOLD_BYTES = 8 * 1024 * 1024; // 8MB

function detectBalanceExhaustion(text: string): "sw" | "anthropic" | null {
  if (SW_BALANCE_ERROR_PATTERN.test(text)) return "sw";
  if (ANTHROPIC_BALANCE_ERROR_PATTERN.test(text)) return "anthropic";
  return null;
}

interface Attachment {
  name: string;
  mimeType: string;
  dataBase64: string;
}

const SUPPORTED_IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);
const UPLOADS_DIR = join(workspaceDir, "uploads");

/**
 * Images and PDFs go straight into the message as inline content blocks too
 * (immediate vision/document reading, no extra tool round-trip needed just to
 * look at them) -- but EVERY attachment, images/PDFs included, also gets
 * saved to workspace/uploads/ under a randomUUID()-prefixed name, so there's
 * always a stable, collision-free file to point the model at for anything
 * beyond just looking (saving it somewhere permanent, attaching it to an
 * email, etc.). Confirmed live (2026-09-05) as a real data-loss incident
 * without this: with no supported way to get a received image's bytes onto
 * disk, Caroline improvised by reading pending-turn-<tabId>.json directly (an
 * internal crash-recovery file that savePendingTurn() overwrites on EVERY
 * submit()) and decoding attachments[0] out of it -- of 10 photos sent
 * across two messages, only the last one hadn't already been overwritten by
 * the second message's own submit() by the time she got to reading it; the
 * other nine were unrecoverable. The unique on-disk path this now provides
 * is immune to that race (and covers every attachment in a batch, not just
 * index 0) -- the explanatory text below tells the model not to reach for
 * that internal file itself.
 */
function formatTimestampForModel(d: Date): string {
  return d.toLocaleString("en-US", {
    weekday: "short", year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", timeZoneName: "short",
  });
}

function saveAttachmentToUploads(a: Attachment): string {
  if (!existsSync(UPLOADS_DIR)) mkdirSync(UPLOADS_DIR, { recursive: true });
  const savedPath = join(UPLOADS_DIR, `${randomUUID()}-${a.name}`);
  writeFileSync(savedPath, Buffer.from(a.dataBase64, "base64"));
  return savedPath;
}

function attachmentToBlocks(a: Attachment): ContentBlockParam[] {
  if (SUPPORTED_IMAGE_TYPES.has(a.mimeType)) {
    const savedPath = saveAttachmentToUploads(a);
    return [
      { type: "image", source: { type: "base64", media_type: a.mimeType as any, data: a.dataBase64 } },
      {
        type: "text",
        text: `[This image is also saved at ${savedPath} -- use that path (e.g. to copy/move it somewhere ` +
          `permanent) instead of reading backend-internal files like pending-turn-*.json for attachment bytes; ` +
          `those are ephemeral crash-recovery state, get overwritten by the next message, and are not a ` +
          `reliable way to retrieve what you were just sent.]`,
      },
    ];
  }
  if (a.mimeType === "application/pdf") {
    const savedPath = saveAttachmentToUploads(a);
    return [
      { type: "document", source: { type: "base64", media_type: "application/pdf", data: a.dataBase64 } },
      { type: "text", text: `[This document is also saved at ${savedPath}.]` },
    ];
  }
  const savedPath = saveAttachmentToUploads(a);
  return [{ type: "text", text: `[Attached file saved to ${savedPath} -- read it if relevant to the request.]` }];
}

// The full set of connection/recovery states a tab can be in -- see
// ChatSession.connState's own doc comment for why this replaced a pile of
// separate booleans. "connected"/"restarting"/"restart_backoff" are tab-local
// (a hang, an MCP crash, an hourly compaction cycle, or repeated generic
// failures backing off) and go out via caroline_status to just this tab.
// "limited"/"billing_blocked" are ACCOUNT-WIDE (every open tab shares one
// Claude subscription) and go out via system_notice to every tab -- see
// setConnState's own comment. "limited" (a usage/session-window cap that
// resets on its own, nothing to fix) is yellow; "billing_blocked" (a
// depleted balance, genuinely actionable) is red.
type ConnStateKind = "connected" | "restarting" | "restart_backoff" | "limited" | "billing_blocked";

type OutEvent =
  // isVoice is only ever attached when message.type === "result" -- it belongs to
  // the turn that's ending, not to the message envelope in general (see queue's
  // own doc comment for why this lives with the specific turn, not smeared across
  // session-level mutable state). The client uses it directly instead of its own
  // positional turnQueue, which proactive (backend-only) turns can desync.
  | { type: "sdk_message"; message: SDKMessage; isVoice?: boolean }
  | { type: "caroline_status"; status: "connected" | "restarting" | "restart_backoff" | "stopped"; reason?: string }
  | { type: "control_stream"; op: string; chunk: string }
  | { type: "control_response"; op: string; ok: boolean; stdout?: string; stderr?: string; requestId?: string }
  | { type: "open_editor"; requestId: string; path: string; kind: "image" | "video" }
  | { type: "open_office_editor"; requestId: string; path: string; config: OfficeConfig }
  | { type: "close_editor"; path: string }
  | { type: "open_login"; requestId: string; error?: string; noAiAtAll?: boolean }
  | { type: "open_payment"; requestId: string; checkoutUrl: string }
  // A static, backend-authored notice shown as a plain assistant bubble, NOT part of
  // any real SDK turn (no queued turn/heartbeat bookkeeping expects a matching
  // "result" for it) -- see handleBalanceExhausted's doc comment for its one
  // current use (a chat turn failing on a depleted balance, caught as a thrown
  // error before the SDK ever produced a real assistant message to show instead).
  // cls defaults to "error" (red lamp) when omitted -- reserved for conditions
  // the user can actually act on (billing_error: depleted balance). A usage/
  // session-window limit is different: it resets on its own, there's nothing
  // to fix, so it's passed as "restarting" (yellow, same as a plain restart)
  // per explicit instruction (2026-09-05).
  | { type: "system_notice"; text: string; cls?: "error" | "restarting" }
  // Sent once per backend-process lifetime, primary tab only (see hasSentVisualModeConfig's
  // call site) -- tells the WPF shell whether/which .xcfa model to warm up in memory for
  // Visual Mode (VisualModeManager.cs). modelPath is null whenever visual mode is disabled
  // in Settings OR unavailable for the current persona/day (see resolveVisualModel) -- the
  // shell then simply has nothing to warm and Visual Mode silently falls back to the normal
  // in-page audio player for that whole run (until the next Caroline restart re-resolves this).
  | { type: "visual_mode_config"; enabled: boolean; modelPath: string | null }
  // Tells the client a turn it did NOT call send() for is about to produce its own
  // "result" event -- see injectProactive's doc comment for the exact desync bug
  // this closes (a proactive turn's result silently stealing a real queued turn's
  // turnQueue slot). The client pushes a matching non-voice placeholder so its
  // turnQueue.shift() in the "result" handler stays 1:1 aligned with the backend's
  // actual turn count, regardless of how many proactive turns interleave.
  | { type: "proactive_turn_queued" };

/**
 * Wraps one Claude Agent SDK streaming-input `query()` session and keeps it
 * alive: a watchdog restarts the underlying session (which respawns every
 * MCP server fresh) if it stops producing messages while a turn is pending,
 * or if the query loop throws. The WebSocket client never has to reconnect
 * or resend anything by hand -- the pending message is replayed into the
 * fresh session automatically.
 */
class ChatSession {
  // isVoice travels WITH each queued message rather than as session-level mutable
  // state -- a single `this.isVoice` flag set at submit() time suffers the exact
  // same race silentTurn's own doc comment describes (two submits landing in the
  // same turn, one overwriting the other before the SDK actually consumes either).
  // Keeping it per-message and only folding it into the current turn's isVoice
  // (see turnIsVoice) at the moment inputStream() actually yields that message
  // is what makes it correct regardless of how many submits pile up first.
  private queue: { message: SDKUserMessage; isVoice: boolean }[] = [];
  /** OR-combined across every queued message actually consumed into the CURRENT
   *  turn (see inputStream) -- true if any of them was real voice input. Reset
   *  to false right after every 'result', same lifecycle as silentTurn. Sent
   *  alongside the 'result' SDK message itself so the client's TTS decision
   *  comes from the backend's own authoritative state instead of a client-side
   *  positional queue that proactive (backend-only) turns can desync -- confirmed
   *  live (2026-09-03) as the actual cause of voice replies silently not being
   *  spoken. */
  private turnIsVoice = false;
  private resolveNext: (() => void) | null = null;
  private ended = false;
  private activeQuery: Query | null = null;
  /** PID of the CURRENT query()'s own underlying CLI process, once identified
   *  (see processReaper.ts -- the SDK exposes no direct handle to it, so this
   *  is diffed from two snapshots of process.pid's direct children taken
   *  right around the query() call). Read (and cleared) by the runLoop catch
   *  block right when THIS instance is being abandoned for a fresh one, so a
   *  confirmed-real failure mode -- the old CLI process simply never exiting
   *  -- gets a scheduled force-kill instead of leaking forever. */
  private cliProcessPid: number | null = null;
  private lastActivity = Date.now();
  /** Set only by real messages from the human (see submit's isRealUser param) -- unlike
   *  lastActivity, proactive/background self-injections don't touch this, so it's a clean
   *  signal for "is the user actually here right now" (see hasLiveDialog). */
  private lastUserActivity = Date.now();
  private turnPending = false;
  private pendingUserText: string | null = null;
  private pendingAttachments: Attachment[] = [];
  private restartTimestamps: number[] = [];
  private watchdogTimer: NodeJS.Timeout;
  private compactionTimer: NodeJS.Timeout;
  /** null = never compacted yet this process lifetime -- compactSessionIfDue
   *  treats that as "due immediately" (covers the startup trigger). Set to
   *  the compaction's own Date.now() afterward, not the timer-tick time, so
   *  a compaction deferred past its hourly tick (turn was active) doesn't
   *  make the NEXT one fire early. */
  private lastCompactedAt: number | null = null;
  /** Hourly tick landed mid-turn -- see maybeCompact/runCompaction. Actually
   *  run once the in-flight turn's 'result' arrives instead of mid-turn. */
  private pendingCompaction = false;
  /** Reentrancy guard for runCompaction() -- the hourly setInterval tick and
   *  the post-result "pendingCompaction was deferred, try now" check (see
   *  maybeCompact) can both fire close together, and forkSession itself can
   *  take a real, non-trivial amount of time on a large session. Without
   *  this, two overlapping runs could both read the same lastCompactedAt and
   *  both fork the same source session, racing each other's
   *  saveTabSessionId/forceRestart calls. */
  private compactionInProgress = false;
  private userStopRequested = false;
  /** Set the first time checkHang() tries interrupt() on a given hang; null
   *  once the turn resolves or the escalation to close() has fired. Lets
   *  checkHang() tell "just detected this hang" from "already tried the
   *  soft path and it didn't work" instead of retrying interrupt() forever. */
  private hangInterruptedAt: number | null = null;
  /** How many times THIS session (this query()/claude.exe/MCP-connection set)
   *  has hung, ever -- deliberately never reset back to 0 for the session's
   *  lifetime (only a fresh session via close()+restart starts at 0 again).
   *  See checkHang()'s doc comment for why this exists: confirmed live that
   *  interrupt() clearing turnPending does NOT mean whatever actually caused
   *  the hang (a wedged MCP connection, most often) got fixed -- the same
   *  session hung 8 separate times over 3.5 hours on 2026-08-30/31 before the
   *  whole process eventually froze solid. A session is allowed exactly one
   *  "maybe it was transient" pass; hanging again means something in this
   *  session's own connections is actually broken and only a full restart
   *  (fresh MCP servers) can fix it. */
  private hangCount = 0;
  /** False from the moment a fresh query() starts until its first 'system'/
   *  'init' SDK message arrives -- confirmed live (2026-08-31) that Caroline's
   *  full MCP set (17 servers, several of them full Chromium instances) can
   *  take well over HANG_TIMEOUT_MS just to finish launching, especially
   *  under concurrent system load. That's cold startup, not a stuck
   *  conversation -- checkHang() must not apply the same 90s "something's
   *  wedged" timeout to it, or every restart just re-triggers the same
   *  not-yet-finished startup and never gets a chance to actually complete
   *  (confirmed live: 6 restarts in 4 minutes, backend never once reached
   *  init, before the external watchdog and the fatal-error dialog kicked
   *  in on what was really still "connecting", not a failure). */
  private hasSeenInit = false;
  /** This tab's own Claude session id, once known -- see runLoop's resume
   *  option and the capture point inside the for-await loop below. */
  private lastSavedSessionId: string | null = null;
  /** Which session id dehydratePreviousTurns() was last called against for
   *  THIS tab -- see runDehydration(). Reset (along with
   *  dehydratedThroughLine below) whenever the live session id changes
   *  (a fresh process lifetime, a resume, or a compaction fork), since a
   *  different .jsonl file means nothing in it has been scanned yet. */
  private dehydratedForSessionId: string | null = null;
  /** How many lines of dehydratedForSessionId's .jsonl are already known
   *  clean/dehydrated -- see dehydrate.ts's own doc comment on why only the
   *  tail past this point can ever need (re-)scanning. 0 = nothing scanned
   *  yet for the current dehydratedForSessionId. */
  private dehydratedThroughLine = 0;
  /** A defensive forkSession() snapshot of the live session, taken right
   *  before EACH turn's own query() starts (and thus starts appending to
   *  the live file) -- see parallel.ts's own doc comment for why this is
   *  the only safe, static basis a parallel branch can resume from. null
   *  when there's no history yet to snapshot (a brand-new conversation) or
   *  the snapshot attempt itself failed. Replaced (old one deleted) at the
   *  top of every runLoop iteration -- never kept longer than one turn. */
  private preTurnSnapshotId: string | null = null;
  /** anthropicEnv/mcpServers/disallowedTools as resolved for the CURRENT
   *  turn -- cached here (not just a runLoop-local) so submitOrBranch() can
   *  reuse the exact same chat-source/tool config for a parallel branch
   *  without re-resolving it (and without threading it through the
   *  submit()/pushMessage() call chain, which has nothing to do with this). */
  private lastAnthropicEnv: Record<string, string> | undefined = undefined;
  private lastMcpServers: Record<string, McpServerConfig> | null = null;
  private lastDisallowedTools: string[] = [];
  /** Per-server retry timers for MCP servers that failed to connect (see the
   *  'system'/'init' handling in runLoop) -- keyed by server name so a
   *  second failure report for the same name doesn't stack a duplicate
   *  retry loop on top of one already running. */
  private mcpReconnectTimers: Map<string, NodeJS.Timeout> = new Map();
  /** Set while an API-level problem (balance exhausted, CLI usage-cap hit) is
   *  blocking replies -- see scheduleApiRetry. Per explicit instruction: these
   *  don't just explain-and-give-up, they retry automatically until the same
   *  original turn actually goes through. Cleared on a successful (non-blocked)
   *  result and on dispose(). */
  private apiRetryTimer: NodeJS.Timeout | null = null;
  /** Set just before force-closing activeQuery specifically to pick up an
   *  own-Anthropic -> sw-proxy chat-source fallback (see subscriptionMode.ts's
   *  markOwnAnthropicExhausted): Options.env is baked into the CLI subprocess
   *  at query() creation and can't change on an already-running session, so
   *  scheduleApiRetry's normal "feed a retry message into the SAME session"
   *  path would just keep failing against the exhausted source forever --
   *  this session needs to actually END so the next while-loop iteration
   *  re-resolves mode and gets a fresh subprocess with the new env. Checked
   *  first in runLoop's catch block so the resulting failure is recognized as
   *  this deliberate, expected restart -- no restart-budget cost, no
   *  misattribution to a generic hang/crash. Cleared as soon as it's acted on. */
  private restartForChatSourceSwitch = false;
  /** Same restart-without-cost mechanism as restartForChatSourceSwitch above,
   *  for the urgent-compaction path (see PROMPT_TOO_LONG_PATTERN's own doc
   *  comment): runUrgentCompaction() sets this right before force-closing
   *  activeQuery so runLoop's catch block recognizes the resulting failure as
   *  this deliberate restart -- no restart-budget cost, no misattribution.
   *  urgentCompactionReplay* carries the turn that hit "Prompt is too long"
   *  across that restart so it can be resubmitted once the now-smaller
   *  session is back up, instead of just silently dropping the user's actual
   *  question. */
  private restartForUrgentCompaction = false;
  private urgentCompactionReplayText: string | null = null;
  private urgentCompactionReplayAttachments: Attachment[] = [];
  /** Same restart-without-cost mechanism as restartForChatSourceSwitch/
   *  restartForUrgentCompaction above -- per explicit instruction (2026-09-08):
   *  directly confirmed live that a running CLI subprocess keeps its
   *  conversation in memory across turns and never re-reads the on-disk
   *  .jsonl mid-lifetime (a turn's cache_read_input_tokens exactly matched
   *  the token count of content already stripped from disk between turns --
   *  see test-dehydrate-live.mjs), so runDehydration()'s in-place rewrite is
   *  otherwise invisible to the live process until SOME restart happens.
   *  Set right after every normal "result" (not just urgent/failure paths)
   *  so a fresh query() -- which DOES read the file fresh at resume -- picks
   *  up the just-dehydrated transcript before the next turn. Unlike
   *  urgentCompaction's flag, this never needs a replay: the turn already
   *  completed and was already delivered to the user. */
  private restartForDehydration = false;
  // Was 10 minutes; shortened per explicit instruction (2026-09-03) -- the
  // subscription/balance this is retrying against can recover at any moment
  // (the user re-logging in, a monthly cap resetting, a top-up landing), and
  // the old interval meant Caroline could sit red for up to 10 minutes after
  // that already happened, with nothing re-checking in the meantime.
  private static readonly API_RETRY_INTERVAL_MS = 90_000;
  /** Most recent rate_limit_event seen from the SDK (a structured, reliable
   *  signal distinct from CC_CLI_LIMIT_PATTERN's text-matching -- see that
   *  pattern's own doc comment). Confirmed live (2026-09-04): a hard usage
   *  cutoff can kill the query() stream with NO text message at all (just
   *  "stream ended unexpectedly", indistinguishable from a genuine hang) --
   *  right after an unusually expensive turn ($13.84, 14 sub-turns), the
   *  session went silent and was eventually killed by our own hang-timeout,
   *  exhausting the restart budget and showing the scary "giving up"
   *  dialog. Tracking this lets a subsequent silent stream-death be
   *  recognized as a limit hit (status 'rejected') instead of a generic
   *  crash, even with no text to match. Never reset back to null once
   *  rejected except by a later event reporting 'allowed'/'allowed_warning'
   *  -- there's no other signal that the cutoff has lifted. This is crash
   *  ATTRIBUTION memory (what did the stream die of?), not UI state -- see
   *  connState for the single source of truth on what the lamp shows. */
  private lastRateLimitInfo: SDKRateLimitInfo | null = null;
  /** Most recent api_retry system message's error field -- see that
   *  handler's own comment. Reset to null on every successful result
   *  (unlike lastRateLimitInfo, which stays 'rejected' until we're
   *  explicitly told otherwise): an api_retry is the CLI's own one-shot
   *  retry-in-progress bookkeeping for a single request, not an
   *  account-wide state that persists across turns. Also crash-attribution
   *  memory, not UI state (see connState). */
  private lastApiRetryError: string | null = null;

  /** Single source of truth for this tab's connection/recovery status --
   *  replaces what used to be five loosely-coordinated booleans
   *  (inErrorRecovery, pendingRestartRecovery, plus an ad hoc
   *  suppressNextConnectedReset patch on top of those) each with its own
   *  reset rule enforced only by convention/comments, not by any single
   *  transition point. Confirmed live (2026-09-05) as the actual cause of a
   *  real bug: inErrorRecovery got set true by scheduleApiRetry and then
   *  immediately flipped back to false by that SAME turn's own terminal
   *  result (the CLI still closes a limit-blocked turn out "successfully"),
   *  sending caroline_status=connected within the same second the limit was
   *  hit -- long before the actual 90s retry ever ran, falsely showing
   *  "connected" while real user messages sat unanswered for ~2 minutes.
   *  All transitions MUST go through setConnState() -- see its own comment
   *  for the full set of scenarios this covers. */
  private connState: { kind: ConnStateKind; reason?: string } = { kind: "connected" };
  /** Armed by setConnState() only for the three MID-STREAM (in-loop,
   *  `continue`-based) interceptions -- billing_error, cc_cli_limit_message,
   *  rate_limit_event 'rejected' -- because those are always immediately
   *  followed by that SAME turn's own terminal "result" (the CLI closes the
   *  turn out normally even though it just returned a limit message instead
   *  of a real answer). Consumed by the very next result's auto-recovery
   *  check so THAT result isn't mistaken for proof of recovery. NOT armed by
   *  the two catch-block variants (a thrown billing error, or a stream death
   *  reclassified via lastRateLimitInfo/lastApiRetryError) -- those tear the
   *  query() down and start a fresh one, so there's no same-turn result to
   *  guard against; arming it there would wrongly suppress the NEXT turn's
   *  genuine recovery instead. */
  private ignoreNextResultRecovery = false;
  /** True while the in-flight turn's SDK messages should not reach the chat UI --
   *  set for internal housekeeping (the periodic memory-backup nudge) that Caroline
   *  still needs to act on, but that isn't a real conversation turn to show the user.
   *
   *  AND-combined across every submit() call since the last 'result' (see submit's
   *  own comment), NOT simply overwritten -- confirmed live (2026-09-01) as a real
   *  bug: the startup greeting (silent=false) queued at 07:01:57, then the hourly
   *  vault-backup reminder (silent=true) queued at 07:02:09 while that first turn
   *  was still cold-starting its MCP servers (query() hadn't consumed either
   *  message yet), both landed in the SAME turn, and the reminder's later
   *  submit() call overwrote silentTurn back to true -- silently swallowing the
   *  greeting's entire visible reply. Starts (and resets after every 'result') at
   *  true = "assume silent until a real submit says otherwise", so a lone silent
   *  submit still behaves exactly as before; only a *mix* of silent and non-silent
   *  submits landing in the same turn now correctly stays visible. */
  private silentTurn = true;

  /** How many times the CURRENT turn has already hit an Anthropic API
   *  content-classifier false-positive refusal (see CLASSIFIER_REFUSAL_PATTERN's
   *  own doc comment) -- 0 = none yet, 1 = one auto-retry already queued and
   *  we're waiting to see if it recurs. Reset to 0 at the start of every
   *  submit() (a genuinely new user turn) and whenever a 'result' arrives, so
   *  it never leaks across turns. Capped at one silent auto-retry: a second
   *  hit in the same turn means it isn't a one-off transient misfire, so
   *  the user gets told plainly instead of Caroline retrying forever. */
  private classifierRefusalRetryCount = 0;

  /** Which tab (WPF-side WebView2 instance) this session belongs to -- see
   *  server.ts's `sessions` map. Used to key this session's own resume
   *  session id and pending-turn file (durability.ts) so multiple tabs
   *  sharing one workspace/cwd don't collide into the same underlying
   *  Claude Code conversation or each other's crash-recovery state. */
  /** Sets THIS session's own connState field and sends the one corresponding
   *  client message -- no cross-tab awareness at all. Never call this
   *  directly for "limited"/"billing_blocked" from outside setConnState:
   *  those are account-wide (every open tab shares one Claude subscription)
   *  and MUST go through setConnState's broadcast loop instead, or every tab
   *  but the one that noticed first shows nothing wrong -- confirmed live
   *  (2026-09-04) as a real bug the first time this was built as a single
   *  per-tab send. */
  private applyConnState(kind: ConnStateKind, reason?: string): void {
    const prevKind = this.connState.kind;
    this.connState = { kind, reason };
    // Every transition, including "connected" -- per explicit instruction
    // (2026-09-06): confirmed live that the client's lamp/status bar can end
    // up showing "restarting" (yellow) while this tab's own connState here
    // is already "connected" -- a genuine backend/client desync, cause not
    // yet pinned down. Without this log there was no record of the
    // "connected" transition at all, only the various restart/limit paths
    // that lead INTO a non-connected state -- impossible to tell whether the
    // send() below even fired, let alone whether the client received it.
    console.error(`[caroline] [connState] tab=${this.tabId} ${prevKind} -> ${kind}${reason ? ` (${reason})` : ""}`);
    switch (kind) {
      case "connected":
        this.send({ type: "caroline_status", status: "connected" });
        break;
      case "restarting":
      case "restart_backoff":
        this.send({ type: "caroline_status", status: kind, reason });
        break;
      case "limited":
        this.send({ type: "system_notice", text: reason ?? "", cls: "restarting" }); // yellow -- resets on its own
        break;
      case "billing_blocked":
        this.send({ type: "system_notice", text: reason ?? "" }); // default cls "error" (red) -- genuinely actionable
        break;
    }
  }

  /** THE single place that changes connState -- every scenario that touches
   *  this tab's status must call this, not set a field or call send()
   *  directly. Covers all of:
   *    - "restarting": a hang, an MCP-crash restart, a compaction cycle --
   *      tab-local, quick, no backoff yet.
   *    - "restart_backoff": repeated generic failures past MAX_RESTARTS_PER_WINDOW
   *      (see handleFailure) -- tab-local, but says so and waits between tries;
   *      NEVER escalates to a blocking dialog (per explicit instruction, 2026-09-05).
   *    - "limited": a usage/session-window cap (billing_error is NOT this --
   *      see "billing_blocked") hit either mid-stream (billing_error/
   *      cc_cli_limit_message/rate_limit_event, armIgnoreNextResult=true --
   *      see ignoreNextResultRecovery's own comment) or reclassified from a
   *      silent stream death in the catch block (armIgnoreNextResult=false,
   *      a fresh query() is already on its way with nothing to guard against).
   *    - "billing_blocked": a depleted balance -- same account-wide treatment,
   *      but red (genuinely actionable, unlike "limited").
   *    - "connected": sent only when connState was NOT already "connected"
   *      (see the call site in the main result handler).
   *  "limited"/"billing_blocked" are ACCOUNT-WIDE -- every open tab shares one
   *  Claude subscription, so this applies the SAME state to every open
   *  session, not just this one (mirrors the old broadcastSystemNotice's
   *  reasoning). When a "connected" call clears one of those two states, it
   *  mirrors that in the other direction too: every OTHER tab that was ALSO
   *  sitting in the same account-wide condition gets nudged back to
   *  "connected" as well, via recoverIfAccountLimited -- otherwise nothing
   *  would ever tell them the account-wide condition lifted, since a plain
   *  restart/hang recovery is normally per-tab only. */
  private setConnState(kind: ConnStateKind, reason?: string, opts: { armIgnoreNextResult?: boolean } = {}): void {
    if (opts.armIgnoreNextResult) this.ignoreNextResultRecovery = true;
    if (kind === "limited" || kind === "billing_blocked") {
      for (const session of sessions.values()) session.applyConnState(kind, reason);
      return;
    }
    const prevKind = this.connState.kind;
    this.applyConnState(kind, reason);
    if (kind === "connected" && (prevKind === "limited" || prevKind === "billing_blocked")) {
      for (const other of sessions.values()) {
        if (other !== this) other.recoverIfAccountLimited();
      }
    }
  }

  /** Called on every OTHER open tab from setConnState's "connected" branch --
   *  see that method's own comment. Does nothing if this tab wasn't ALSO
   *  sitting in the same account-wide condition (e.g. it's mid-restart for
   *  its own unrelated tab-local reason, which this must not clobber).
   *  Applies locally only (applyConnState, not setConnState) -- this IS the
   *  broadcast fan-out, so it must not re-trigger another one. */
  private recoverIfAccountLimited(): void {
    if (this.connState.kind === "limited" || this.connState.kind === "billing_blocked") {
      this.applyConnState("connected");
    }
  }

  constructor(private readonly send: (event: OutEvent) => void, private readonly tabId: string) {
    this.watchdogTimer = setInterval(() => this.checkHang(), WATCHDOG_INTERVAL_MS);
    this.compactionTimer = setInterval(() => this.maybeCompact(), COMPACTION_CHECK_INTERVAL_MS);
    setTimeout(() => this.maybeCompact(), COMPACTION_STARTUP_DELAY_MS);
    void this.runLoop();
  }

  /** Hourly tick (or the startup-delay one-shot above). Never runs mid-turn,
   *  AND never while the user still seems actively engaged with this tab
   *  (see hasLiveDialog) -- see compaction.ts's own doc comment for why the
   *  live query() can't have its resume target swapped without a restart,
   *  so a mid-turn swap here would just get clobbered by the SAME turn's
   *  own session_id capture. Confirmed live (2026-09-05): turnPending alone
   *  isn't a safe enough idle signal -- it's briefly false BETWEEN turns of
   *  an ongoing multi-step task the user is still actively driving (a
   *  facebook-automation session, in the incident that surfaced this), so a
   *  compaction fork could land mid-task even though no single turn was
   *  interrupted. */
  private maybeCompact(): void {
    if (this.hasLiveDialog()) {
      this.pendingCompaction = true;
      return;
    }
    void this.runCompaction();
  }

  private async runCompaction(): Promise<void> {
    if (this.compactionInProgress) {
      console.error(`[caroline] compaction: tab ${this.tabId} already has a run in progress -- skipping this trigger`);
      return;
    }
    const sessionId = loadTabSessionId(workspaceDir, this.tabId);
    if (!sessionId) return; // nothing resumed yet for this tab -- nothing to compact
    this.compactionInProgress = true;
    try {
      const result = await compactSessionIfDue(workspaceDir, sessionId, this.lastCompactedAt);
      if (!result) return;
      this.lastCompactedAt = result.compactedAt;
      saveTabSessionId(workspaceDir, this.tabId, result.newSessionId);
      console.error(`[caroline] compaction: tab ${this.tabId} forked ${sessionId} -> ${result.newSessionId}, restarting session, descendantProcesses=${await describeDescendantProcesses(process.pid)}`);
      this.forceRestart();
    } catch (err) {
      // Background, optional housekeeping (per explicit instruction,
      // 2026-09-05) -- any failure here must never affect the live
      // conversation. Log and retry next hour, nothing more.
      console.error(`[caroline] compaction failed for tab ${this.tabId} (ignored, will retry next hour):`, err);
    } finally {
      this.compactionInProgress = false;
    }
  }

  /**
   * Urgent counterpart to runCompaction() -- triggered the moment a
   * "Prompt is too long" turn is detected (see PROMPT_TOO_LONG_PATTERN's own
   * doc comment), not on the hourly schedule: the live session is already
   * too big to even load, so waiting isn't an option. Purely algorithmic
   * (compactSessionIfDue does local file work only, no model call), so this
   * runs identically whether or not any chat source currently has usable
   * tokens -- per explicit instruction, that's the whole point of using
   * Caroline's own compaction here instead of leaning on the CLI's.
   * `replayText`/`replayAttachments` are the turn that hit the failure
   * (server.ts's caller captures them from this.pendingUserText/
   * pendingAttachments before they're cleared) -- resubmitted once the
   * now-smaller session is back up, via restartForUrgentCompaction's own
   * catch-block handling in runLoop, so the user's actual question still
   * gets answered instead of silently dropped.
   */
  private async runUrgentCompaction(replayText: string | null, replayAttachments: Attachment[]): Promise<void> {
    if (this.compactionInProgress) {
      console.error(`[caroline] urgent compaction: tab ${this.tabId} already has a compaction in progress -- skipping (the in-flight one will still land)`);
      return;
    }
    const sessionId = loadTabSessionId(workspaceDir, this.tabId);
    if (!sessionId) {
      console.error(`[caroline] urgent compaction: tab ${this.tabId} has no resumed session to compact -- nothing to do`);
      return;
    }
    this.compactionInProgress = true;
    try {
      const result = await compactSessionIfDue(workspaceDir, sessionId, this.lastCompactedAt, true);
      if (!result) {
        // Only happens if compactSessionIfDue itself decided there was
        // nothing to do despite force=true -- not currently a real code
        // path, but handled rather than silently stranding the user.
        console.error(`[caroline] urgent compaction: tab ${this.tabId} compactSessionIfDue returned null despite force=true`);
        return;
      }
      this.lastCompactedAt = result.compactedAt;
      saveTabSessionId(workspaceDir, this.tabId, result.newSessionId);
      console.error(`[caroline] urgent compaction: tab ${this.tabId} forked ${sessionId} -> ${result.newSessionId}, restarting session`);
      this.urgentCompactionReplayText = replayText;
      this.urgentCompactionReplayAttachments = replayAttachments;
      this.restartForUrgentCompaction = true;
      this.activeQuery?.close();
    } catch (err) {
      // Unlike routine runCompaction(), this can't just "log and retry next
      // hour" -- the user is actively waiting on this turn. Fall back to the
      // generic recovery nudge (same one billing/rate-limit paths use) so
      // Caroline at least keeps trying instead of leaving the status bar
      // stuck on "Urgent compaction..." forever.
      console.error(`[caroline] urgent compaction: tab ${this.tabId} failed -- falling back to generic retry:`, err);
      this.setConnState("restarting", "Compaction failed, retrying...");
      this.scheduleApiRetry("urgent_compaction_failed", this.turnIsVoice);
    } finally {
      this.compactionInProgress = false;
    }
  }

  /**
   * Per explicit instruction (2026-09-08): every turn, not just on the hourly/
   * urgent compaction schedule, the PREVIOUS turn's raw image/document bytes
   * get replaced with a link to a file on disk -- see dehydrate.ts's own doc
   * comment for exactly why this is safe to do IN PLACE (unlike
   * compaction.ts, which only ever touches a fork) and for the two call
   * sites this is invoked from. `sessionId` is whatever this tab's CURRENT
   * live session id is -- undefined/null means nothing has been resumed/
   * started yet, nothing to do.
   *
   * Also runs agePreviousTurnsInPlace right after (2026-09-09, explicit
   * instruction): compaction.ts's recent-content byte budget was only ever
   * enforced by the hourly/urgent fork-based passes -- confirmed live that
   * real content can blow past it in minutes during a busy stretch, long
   * before the next hourly tick ever gets a chance to trim it back down.
   * Runs AFTER dehydration on purpose -- images/documents should already be
   * gone by the time it looks at what's left. Separate try/catch from
   * dehydration above so a failure in one doesn't skip the other.
   */
  private async runDehydration(sessionId: string | null | undefined): Promise<void> {
    if (!sessionId) return;
    if (sessionId !== this.dehydratedForSessionId) {
      console.error(`[caroline] dehydrate: tab ${this.tabId} switching tracked session ${this.dehydratedForSessionId ?? "(none)"} -> ${sessionId}, rescanning from line 0`);
      this.dehydratedForSessionId = sessionId;
      this.dehydratedThroughLine = 0;
    }
    try {
      const outcome = await dehydratePreviousTurns(workspaceDir, sessionId, this.dehydratedThroughLine);
      this.dehydratedThroughLine = outcome.newThroughLine;
      if (outcome.changed) {
        console.error(`[caroline] dehydrate: tab ${this.tabId} session ${sessionId} entriesChanged=${outcome.entriesChanged} linesRescanned=${outcome.linesRescanned}`);
      }
    } catch (err) {
      // Same "never take down the live conversation" guarantee as
      // compaction.ts -- this is optional housekeeping, log and move on.
      console.error(`[caroline] dehydrate: tab ${this.tabId} session ${sessionId} failed (ignored, will retry next turn):`, err);
    }
    try {
      const budgetOutcome = await agePreviousTurnsInPlace(workspaceDir, sessionId);
      if (budgetOutcome.changed) {
        console.error(`[caroline] age-budget: tab ${this.tabId} session ${sessionId} linesCollapsed=${budgetOutcome.linesCollapsed}`);
      }
    } catch (err) {
      console.error(`[caroline] age-budget: tab ${this.tabId} session ${sessionId} failed (ignored, will retry next turn):`, err);
    }
  }

  /**
   * Entry point for a REAL incoming user message (the ws `user_message`
   * handler and the tab-agnostic HTTP `/api/message` endpoint) -- NOT for
   * internal replays/proactive nudges, which must keep calling submit()
   * directly. See the approved parallel-turns plan (foamy-sniffing-pixel.md)
   * for the full design. If no turn is in flight, behaves exactly like
   * submit() always has. If one IS in flight, asks a cheap model call
   * (classifyParallelSafety) whether this new message is independent enough
   * to work on right now, in a throwaway branch forked off this turn's own
   * defensive pre-turn snapshot, instead of just queueing behind it.
   * Attachments always fall back to plain sequential queueing -- carrying
   * them into a branch isn't handled here, and silently dropping them would
   * be worse than just queueing normally.
   */
  submitOrBranch(text: string, attachments: Attachment[] = [], isVoice = false): void {
    if (
      !this.turnPending ||
      this.pendingUserText === null ||
      !this.preTurnSnapshotId ||
      !this.lastMcpServers ||
      attachments.length > 0
    ) {
      this.submit(text, attachments, true, false, isVoice);
      return;
    }
    const inFlightTaskText = this.pendingUserText;
    const snapshotId = this.preTurnSnapshotId;
    const anthropicEnv = this.lastAnthropicEnv;
    const mcpServers = this.lastMcpServers;
    const disallowedTools = this.lastDisallowedTools;
    void (async () => {
      const isParallel = await classifyParallelSafety(anthropicEnv, workspaceDir, inFlightTaskText, text);
      if (!isParallel) {
        console.error(`[caroline] [parallel] tab=${this.tabId} classifier said SEQUENTIAL -- queueing normally: ${truncateForLog(text)}`);
        this.submit(text, attachments, true, false, isVoice);
        return;
      }
      let branchSessionId: string;
      try {
        const forked = await forkSession(snapshotId, { dir: workspaceDir });
        branchSessionId = forked.sessionId;
      } catch (err) {
        console.error(`[caroline] [parallel] tab=${this.tabId} failed to fork branch off snapshot ${snapshotId} -- falling back to sequential:`, err);
        this.submit(text, attachments, true, false, isVoice);
        return;
      }
      console.error(`[caroline] [parallel] tab=${this.tabId} branch ${branchSessionId} spawned for: ${truncateForLog(text)}`);
      runParallelBranch({ workspaceDir, branchSessionId, text, anthropicEnv, mcpServers, disallowedTools })
        .then((answer) => {
          console.error(`[caroline] [parallel] tab=${this.tabId} branch ${branchSessionId} reporting back (answer=${answer !== null ? "ok" : "null"})`);
          this.injectProactive(buildBranchReportText(text, answer), false);
        })
        .catch((err) => {
          console.error(`[caroline] [parallel] tab=${this.tabId} branch ${branchSessionId} promise chain rejected unexpectedly:`, err);
        })
        .finally(() => {
          void deleteSessionFile(workspaceDir, branchSessionId);
        });
    })();
  }

  submit(text: string, attachments: Attachment[] = [], isRealUser = true, silent = false, isVoice = false): void {
    // AND-combine, don't overwrite -- see silentTurn's own doc comment for
    // the exact race this fixes (a later silent submit landing in the same
    // in-flight turn as an earlier non-silent one must not suppress it).
    this.silentTurn = this.silentTurn && silent;
    this.classifierRefusalRetryCount = 0;
    this.pendingUserText = text;
    this.pendingAttachments = attachments;
    this.turnPending = true;
    this.lastActivity = Date.now();
    if (isRealUser) this.lastUserActivity = Date.now();
    // Persisted to disk (not just kept in memory) so a FULL app restart --
    // not just this backend's own in-process watchdog restart, which
    // already replays from memory in handleFailure -- can still notice an
    // unanswered turn and resume it. See takePendingTurn's call site below.
    savePendingTurn(workspaceDir, this.tabId, text, attachments);
    this.pushMessage(text, attachments, isVoice);
  }

  /**
   * Injects a message with no user action behind it -- a due reminder --
   * so Caroline continues the conversation proactively instead of waiting
   * for the next thing the user types. Returns false (does nothing) once
   * the session has ended, so the caller (the due-check loop) knows not to
   * mark the reminder fired and can retry against whatever session
   * replaces this one.
   */
  injectProactive(text: string, silent = false): boolean {
    if (this.ended) return false;
    // The client's turnQueue (chat.js) is a plain FIFO the client itself pushes
    // to only from its own send() calls -- it has no other way to learn that a
    // proactive turn (reminder, ratatosk nudge, startup greeting, etc.) is about
    // to consume a "result" event too. Without this, ANY proactive turn landing
    // between a real user turn's submit and its result silently steals that
    // result via the client's blind turnQueue.shift(), leaving the real turn's
    // isVoice flag and assistantText misattributed to nothing -- confirmed live
    // (2026-09-03) as the actual cause of "voice replies stopped working
    // entirely": a memory-backup reminder fired between a voice message and its
    // reply, and the reply's TTS never fired because isVoice was lost in the
    // shuffle. Mirrors the "stopped" case's own turnQueue.unshift() placeholder
    // (see chat.js) -- same principle, applied to every proactive turn, not
    // just interrupts.
    console.error(`[caroline] [proactive] injecting turn (silent=${silent}): ${text.slice(0, 80)}`);
    this.send({ type: "proactive_turn_queued" });
    this.submit(text, [], false, silent);
    return true;
  }

  /** Snapshot for the local HTTP control API's GET /api/status -- also
   *  polled externally by the WPF shell's BackendHealthWatchdog (see
   *  MainWindow.xaml.cs), independent of anything running inside this
   *  process's own event loop, specifically so a full event-loop freeze
   *  (nothing in here can self-report that) still gets caught and recovered
   *  from the outside. hangCount lets that external check -- and a human
   *  looking at /api/status directly -- see this session has already needed
   *  rescuing before, not just whatever the current instant looks like. */
  getStatus() {
    return {
      ended: this.ended,
      turnPending: this.turnPending,
      lastActivityMs: Date.now() - this.lastActivity,
      lastUserActivityMs: Date.now() - this.lastUserActivity,
      hangCount: this.hangCount,
      connState: this.connState,
      // Per explicit instruction (2026-09-08): the external, per-tab
      // BackendHealthWatchdog needs this to recover ONLY the one stuck tab
      // (kill just this pid's tree via AppBrowserHost, in-process -- see
      // BackendHealthWatchdog.cs) instead of the whole shared backend
      // process, which used to take every other tab down with it. null
      // whenever this tab's own CLI process hasn't been identified yet
      // (see cliProcessPid's own doc comment) -- the caller must treat that
      // as "nothing to target", never guess.
      cliProcessPid: this.cliProcessPid,
    };
  }

  /** Forces this session down the same way checkHang's own escalation does
   *  (close() -> runLoop's catch -> handleFailure -> fresh session), for a
   *  human or a script to trigger directly over /api/control instead of
   *  having to kill OS processes by hand -- see server.ts's "force_restart"
   *  control op. */
  forceRestart(): void {
    console.error("[caroline] forceRestart() called via local control API");
    try {
      this.activeQuery?.close();
    } catch (err) {
      console.error("[caroline] close() threw during forceRestart():", err);
    }
  }

  /**
   * True if a human seems to be actively present right now: a turn is
   * in-flight, or they sent something within idleThresholdMs. Background
   * reminders check this and defer themselves (see server.ts's due-check
   * callback) instead of interrupting an ongoing back-and-forth; priority
   * reminders ignore it entirely.
   *
   * restart_backoff overrides turnPending to false regardless -- per explicit
   * instruction (2026-09-07): a session stuck repeatedly failing before it can
   * even reach 'init' (handleFailure's restart-budget escalation) leaves
   * turnPending stuck true forever, since nothing ever produces the "result"
   * that would normally clear it (a resubmitted pending turn is replayed via
   * pushMessage, not a fresh submit(), so it never resets on its own either).
   * That's a broken retry loop, not a live conversation -- confirmed live as
   * the actual reason a 46.5MB session's own compaction (which would have
   * fixed the problem) never got to run: hasLiveDialog() looked permanently
   * "active" to both the hourly tick and the deferred pendingCompaction check.
   */
  hasLiveDialog(idleThresholdMs = 5 * 60_000): boolean {
    if (this.connState.kind === "restart_backoff") return false;
    return this.turnPending || Date.now() - this.lastUserActivity < idleThresholdMs;
  }

  /**
   * User-requested cancellation of whatever's currently running (a long
   * tool call, a slow response) -- unlike checkHang's interrupt, this isn't
   * a failure: it's not counted against restartTimestamps and doesn't log
   * or broadcast a "recovering" status, it just clears turnPending so the
   * UI unblocks. See runLoop's catch block for where userStopRequested is
   * consumed.
   */
  stop(): void {
    if (!this.turnPending) return;
    console.error("[caroline] stop() called -- user requested cancellation of in-flight turn");
    this.userStopRequested = true;
    this.activeQuery?.interrupt().catch((err) => console.error("[caroline] stop(): interrupt() failed (ignored):", err));
  }

  dispose(): void {
    console.error("[caroline] dispose() called -- ending session");
    this.ended = true;
    clearInterval(this.watchdogTimer);
    clearInterval(this.compactionTimer);
    this.clearMcpReconnectTimers();
    this.clearApiRetryTimer();
    this.activeQuery?.interrupt().catch((err) => console.error("[caroline] dispose(): interrupt() failed (ignored):", err));
    this.resolveNext?.();
    if (this.preTurnSnapshotId) {
      void deleteSessionFile(workspaceDir, this.preTurnSnapshotId);
      this.preTurnSnapshotId = null;
    }
  }

  private clearApiRetryTimer(): void {
    if (this.apiRetryTimer) {
      clearTimeout(this.apiRetryTimer);
      this.apiRetryTimer = null;
    }
  }

  /** Per explicit instruction: an API-level problem (balance exhausted, CLI usage-cap
   *  hit) doesn't just explain-and-give-up -- retry automatically until it actually
   *  goes through. Always nudges with a generic "continue from where you left off"
   *  internal message, never a literal resubmission of whatever text happened to be
   *  pending -- per explicit instruction (2026-09-07): re-sending the ORIGINAL text
   *  reads to the model as a brand new request, not a continuation, and confirmed
   *  live as the actual reason an interrupted multi-step task didn't resume once the
   *  limit cleared (the model has full context of what it was doing either way, from
   *  this same session's own history -- it just needs to be told to pick it back up,
   *  not asked to redo the original ask from scratch). No "report the outcome"
   *  framing either -- tasks vary too widely for that to make sense as a blanket
   *  instruction. Silent/not-a-real-user-turn either way: this is Caroline resuming
   *  on her own, not the user saying anything. */
  private scheduleApiRetry(reason: string, isVoice: boolean): void {
    // Confirmed live (2026-09-05): do NOT clear+reschedule when a retry is
    // already pending -- every restart that happens while still blocked
    // (compaction fires roughly hourly, and used to re-arm this on every
    // single one via lastRateLimitInfo staying 'rejected') kept resetting
    // this timer back to the full interval, so it could go HOURS without
    // ever actually firing even though the log showed a fresh "scheduling
    // retry in 90000ms" each time -- the timer was real, it just never got
    // to survive 90 seconds uninterrupted. Once a retry is already
    // ticking, later calls (from an unrelated restart, or the same
    // continuing block) are no-ops -- let the existing one run its course.
    if (this.apiRetryTimer) {
      console.error(`[caroline] [api-retry] retry already pending -- not resetting the timer (reason=${reason})`);
      return;
    }
    console.error(`[caroline] [api-retry] scheduling retry in ${ChatSession.API_RETRY_INTERVAL_MS}ms (reason=${reason})`);
    this.apiRetryTimer = setTimeout(() => {
      // Must null this out BEFORE doing anything else -- a fired timeout's
      // handle is no longer valid, but the field otherwise stays non-null
      // forever, which would make the guard above refuse every future
      // retry (including this one failing again and needing a fresh
      // schedule) permanently after the very first fire.
      this.apiRetryTimer = null;
      if (this.ended) return;
      console.error(`[caroline] [api-retry] retrying now (reason=${reason})`);
      this.submit(
        "[Internal: automatic recheck after an API/subscription limit blocked a previous turn -- continue from " +
          "wherever you left off.]",
        [], false, true, isVoice,
      );
    }, ChatSession.API_RETRY_INTERVAL_MS);
  }

  /** Same status-bar-only/yellow-lamp/auto-retry treatment as cc_cli_limit_message
   *  (distinct from billing_error's red -- a usage-window limit resets on its own,
   *  there's nothing to fix), driven by the SDK's own structured rate_limit_info
   *  instead of matched text -- see lastRateLimitInfo's own doc comment.
   *
   *  `chatSource`/`swLoggedIn` (the SAME mode runLoop resolved for the turn that
   *  just got rejected -- callers pass it straight through) let this fall back to
   *  the user's SquirrelWisdom account instead of just waiting out own-Anthropic's
   *  usage window: see subscriptionMode.ts's markOwnAnthropicExhausted doc comment.
   *  A rate limit on chatSource "sw-proxy" itself has nowhere further to fall back
   *  to, so it's left alone -- same wait-and-retry as before.
   *
   *  Returns whether it fell back -- an IN-STREAM caller (still on the now-stale
   *  session) must force-close it (see restartForChatSourceSwitch's own doc
   *  comment) for the fallback to actually take effect; a catch-block caller is
   *  already on its way to a fresh iteration regardless and can ignore it. */
  private handleRateLimitRejected(source: string, info: SDKRateLimitInfo, chatSource: ChatSource, swLoggedIn: boolean): boolean {
    const fellBackToSw = (chatSource === "own-anthropic-oauth" || chatSource === "own-anthropic-key") && swLoggedIn;
    if (fellBackToSw) markOwnAnthropicExhausted(info.resetsAt);
    // Status-bar/system_notice text is UI chrome, not a chat reply -- always
    // English, regardless of what language the conversation itself is in
    // (see detectRecentLanguage for that separate concern).
    const resetText = info.resetsAt ? ` Resets: ${new Date(info.resetsAt).toLocaleString("en-US")}.` : "";
    const typeText = info.rateLimitType ? ` (${info.rateLimitType})` : "";
    const text = fellBackToSw
      ? `Hit your own Anthropic account's usage limit${typeText}.${resetText} Switching to SquirrelWisdom for now -- I'll switch back automatically.`
      : `Hit the Claude usage limit${typeText}.${resetText} Retrying automatically.`;
    console.error(`[caroline] rate limit rejected (source=${source}, chatSource=${chatSource}): ${JSON.stringify(info)} fellBackToSw=${fellBackToSw}`);
    this.setConnState("limited", text);
    this.scheduleApiRetry(`rate_limit_rejected:${source}`, this.turnIsVoice);
    return fellBackToSw;
  }

  private clearMcpReconnectTimers(): void {
    for (const t of this.mcpReconnectTimers.values()) clearTimeout(t);
    this.mcpReconnectTimers.clear();
  }

  /**
   * Keeps retrying reconnectMcpServer(name) against the query that reported
   * the failure, on a capped exponential backoff, until it succeeds -- so a
   * server that's down for a moment (its target not up yet, a transient
   * launch race) comes back on its own instead of staying unavailable for
   * the rest of the session, and WITHOUT tearing down the whole session the
   * way throwing from the init handler used to (see runLoop's 'system'/
   * 'init' handling -- that used to restart Caroline entirely for a single
   * unrelated server being down, which just failed the same way on every
   * restart and produced an infinite "recovering session..." loop).
   *
   * Bound to the specific `query` instance active when the failure was
   * reported: if that query is no longer this.activeQuery by the time a
   * retry fires (session restarted for an unrelated reason, or ended), the
   * retry loop for it is abandoned -- the new session's own init handling
   * will report and schedule fresh retries if the server is still down.
   */
  private scheduleMcpReconnect(q: Query, name: string, attempt = 0): void {
    if (this.mcpReconnectTimers.has(name)) return; // already retrying this one
    const delayMs = Math.min(15_000 * 2 ** attempt, 5 * 60_000);
    const timer = setTimeout(async () => {
      this.mcpReconnectTimers.delete(name);
      if (this.ended || this.activeQuery !== q) return; // superseded by a full session restart
      try {
        await q.reconnectMcpServer(name);
        console.log(`[caroline] MCP server "${name}" reconnected`);
      } catch (err) {
        console.error(`[caroline] MCP server "${name}" still unavailable, retrying in ${Math.round(delayMs / 1000)}s:`, err);
        this.scheduleMcpReconnect(q, name, attempt + 1);
      }
    }, delayMs);
    this.mcpReconnectTimers.set(name, timer);
  }

  private pushMessage(text: string, attachments: Attachment[] = [], isVoice = false): void {
    // Every message is stamped with when it was actually sent -- without
    // this, a resumed conversation (continue: true, possibly days later)
    // gives no way to tell "today" from "three days ago" apart from
    // whatever the message text itself happens to say. Combined with the
    // time tool for "what's the date right now", this lets her reason
    // about elapsed time correctly instead of treating the whole history
    // as having just happened.
    let sentLine = `[Sent: ${formatTimestampForModel(new Date())}`;
    // Per explicit instruction: voice-transcribed text can contain
    // transcription errors a typed message wouldn't -- flagging it here
    // (not just once in a system prompt) means the note travels with the
    // specific message it applies to, visible in history on a resumed
    // conversation too, not just in the moment.
    if (isVoice) sentLine += ", via voice input -- may contain transcription errors";
    sentLine += "]";
    const content: ContentBlockParam[] = [{ type: "text", text: sentLine }];
    content.push(...attachments.flatMap(attachmentToBlocks));
    if (text) content.push({ type: "text", text });
    console.error(`[caroline] [transcript] pushMessage (outgoing, isVoice=${isVoice}): ${truncateForLog(text)}${attachments.length ? ` (+${attachments.length} attachment(s))` : ""}`);
    this.queue.push({
      message: { type: "user", message: { role: "user", content }, parent_tool_use_id: null },
      isVoice,
    });
    this.resolveNext?.();
  }

  private async *inputStream(): AsyncIterable<SDKUserMessage> {
    while (!this.ended) {
      if (this.queue.length === 0) {
        await new Promise<void>((resolve) => {
          this.resolveNext = resolve;
        });
        this.resolveNext = null;
        continue;
      }
      // Per explicit instruction (2026-09-08): the PREVIOUS turn's raw
      // image/document bytes get dehydrated to disk every turn, not just on
      // the hourly/urgent schedule -- see runDehydration()'s own doc
      // comment. Awaited HERE, before shifting/yielding the next queued
      // item, is exactly what makes this safe to do in place: the CLI only
      // ever asks this generator for its next prompt once it has fully
      // finished (and flushed to disk) the PREVIOUS turn, so no turn is in
      // flight right now -- and the CLI won't see the NEXT turn at all until
      // this await resolves, so the rewrite is always complete first.
      await this.runDehydration(this.lastSavedSessionId);
      const item = this.queue.shift()!;
      this.turnIsVoice = this.turnIsVoice || item.isVoice;
      console.error(`[caroline] [queue] consuming queued message isVoice=${item.isVoice} -> turnIsVoice=${this.turnIsVoice}`);
      yield item.message;
    }
  }

  /**
   * This tab's stored resume session id, if any -- or, for the primary tab
   * specifically, a one-time migration fallback to whatever Claude Code
   * session was most recently active for this workspace BEFORE multi-tab
   * existed (see durability.ts's findMostRecentClaudeSessionId). Without
   * this, a user upgrading from a pre-multi-tab install would silently lose
   * their entire existing conversation the moment the primary tab first
   * starts a session under the new resume-by-id scheme, since continue:true
   * never left behind a stored id to resume from. Persists whatever it
   * finds immediately so this migration path only ever runs once per tab.
   */
  private resolveResumeSessionId(): string | undefined {
    const stored = loadTabSessionId(workspaceDir, this.tabId);
    if (stored) return stored;
    if (this.tabId !== PRIMARY_TAB_ID) return undefined;
    const migrated = findMostRecentClaudeSessionId(workspaceDir);
    if (migrated) {
      console.error(`[caroline] primary tab: no stored session id yet -- migrating pre-multi-tab conversation ${migrated}`);
      saveTabSessionId(workspaceDir, this.tabId, migrated);
    }
    return migrated ?? undefined;
  }

  private async runLoop(): Promise<void> {
    while (!this.ended) {
      try {
        // A fresh query() below means fresh MCP connections -- reset the
        // per-session hang counter now, at the top of this iteration, not
        // just wherever the old session actually got torn down (its own
        // checkHang() escalation, a thrown error, anything) so it can never
        // be left over from the previous session's hangs.
        this.hangCount = 0;
        this.hasSeenInit = false;
        console.error("[caroline] runLoop: starting a fresh session (hangCount reset)");

        // Own-Anthropic (OAuth, then a manually-pasted key) always wins when
        // available; "sw-proxy" points the CLI's own HTTP client at
        // Camerlengo's /v1/messages instead of api.anthropic.com (see
        // subscriptionMode.ts).
        const mode = await resolveMode(workspaceDir);
        // "none" means query() is about to fail on its very first real request no
        // matter what -- there's no chat source to even try. Per explicit
        // instruction (2026-09-07): this used to be left for the CLI's own request
        // to fail "reactively" (per a since-removed comment here claiming that was
        // "the trigger for the credentials-needed UI"), but nothing ever actually
        // implemented that trigger -- openLoginRequest is only ever called from a
        // SW-gated TOOL call or the Settings button, and the model can't run a tool
        // at all when there's no chat source to run it WITH. A new user with no
        // SquirrelWisdom login and no Anthropic account of their own would just
        // loop through handleFailure's generic restart/backoff forever, with the
        // login window never opening on its own. Checked here, before query()
        // creation, so it's native/deterministic instead of depending on the model.
        if (mode.chatSource === "none") {
          const gate = requireSwOrPrompt(this.send, true);
          if (!gate.ok) console.error(`[caroline] runLoop: chatSource=none -- ${gate.message}`);
        }
        let anthropicEnv: Record<string, string> | undefined;
        try {
          anthropicEnv = await buildOptionsEnv(workspaceDir, mode);
        } catch (err) {
          console.error("[caroline] failed to build chat-source env, falling through to no chat source:", err);
        }
        console.error(`[caroline] runLoop: chatSource=${mode.chatSource} swLoggedIn=${mode.swLoggedIn} envOverride=${anthropicEnv ? "yes" : "no"} -- creating query()`);
        this.lastAnthropicEnv = anthropicEnv;

        // No explicit mcpServers here: cwd is Caroline's own workspace
        // directory, a normal Claude Code project dir with its own
        // .mcp.json / Skills/ -- the CLI discovers both the same way it
        // does for any project, so add/remove is just editing those files
        // (or `claude mcp add/remove` against this cwd).
        // MCP servers come from user scope (registered once by
        // ensureWorkspace()), not this cwd's .mcp.json -- see workspace.ts
        // for why. cwd still matters for CLAUDE.md / Skills/ discovery.
        let resumeSessionId = this.resolveResumeSessionId();
        // Proactive, SDK-INDEPENDENT compaction check -- per explicit instruction
        // (2026-09-08): our own compaction is our own decision, not something that
        // should wait on or depend on any signal from the SDK/CLI. A plain fs.stat
        // on the transcript we're about to resume, before query() is ever created --
        // see getSessionFileSizeBytes's own doc comment for the confirmed-live
        // silent-stall incident (326s, zero SDK output) this specifically guards
        // against, which the reactive PROMPT_TOO_LONG_PATTERN detection further
        // down can't catch on its own since it depends on a message that isn't
        // guaranteed to ever arrive.
        if (resumeSessionId) {
          const sizeBytes = await getSessionFileSizeBytes(workspaceDir, resumeSessionId);
          console.error(`[caroline] [urgent-compaction] tab=${this.tabId} pre-resume size check: session=${resumeSessionId} sizeBytes=${sizeBytes ?? "unknown"} thresholdBytes=${URGENT_COMPACTION_SIZE_THRESHOLD_BYTES}`);
          if (sizeBytes !== null && sizeBytes >= URGENT_COMPACTION_SIZE_THRESHOLD_BYTES) {
            console.error(`[caroline] [urgent-compaction] tab=${this.tabId} session ${resumeSessionId} is ${sizeBytes} bytes, over threshold -- compacting BEFORE attempting to resume, no query() created yet`);
            this.setConnState("restarting", "Urgent compaction...");
            try {
              const result = await compactSessionIfDue(workspaceDir, resumeSessionId, this.lastCompactedAt, true);
              if (result) {
                this.lastCompactedAt = result.compactedAt;
                saveTabSessionId(workspaceDir, this.tabId, result.newSessionId);
                console.error(`[caroline] [urgent-compaction] tab=${this.tabId} pre-resume compaction done: ${resumeSessionId} -> ${result.newSessionId}`);
                resumeSessionId = result.newSessionId;
              } else {
                console.error(`[caroline] [urgent-compaction] tab=${this.tabId} pre-resume compaction returned null despite force=true -- resuming the original session as-is`);
              }
            } catch (err) {
              // Same "never take down the live conversation" guarantee as
              // compactSessionIfDue's own doc comment -- log and resume the
              // original session anyway; the reactive PROMPT_TOO_LONG_PATTERN
              // path further down is still there as a backstop if this
              // particular attempt turns out to still be too big.
              console.error(`[caroline] [urgent-compaction] tab=${this.tabId} pre-resume compaction failed (resuming original session as-is):`, err);
            }
          }
          // Per-turn dehydration's own FIRST opportunity this query()
          // lifetime -- see runDehydration()'s and inputStream()'s own doc
          // comments for why every subsequent turn is handled there
          // instead. Done here specifically (before query() creation, no
          // live CLI process for this lifetime exists yet at all) so there
          // is nothing to race: whatever the CLI reads once it starts up
          // and resumes resumeSessionId is guaranteed to already be the
          // rewritten file, not a stale in-flight read.
          await this.runDehydration(resumeSessionId);
        }
        const queryStartedAt = Date.now();
        console.error(`[caroline] runLoop: about to create query() -- resume=${resumeSessionId ?? "(none)"} tab=${this.tabId} descendantProcesses=${await describeDescendantProcesses(process.pid)}`);
        // Adds schedule_reminder/list_reminders/cancel_reminder and
        // open_file as in-process tools -- merges with (doesn't replace)
        // the user-scope caroline-* servers discovered from cwd.
        const mcpServers: Record<string, McpServerConfig> = {
          "caroline-scheduler": createSchedulerTool(workspaceDir),
          "caroline-files": createFileOpenerTool(),
          "caroline-viewer": createViewerTool((event) => this.send(event)),
          "caroline-login": createLoginTool((event) => this.send(event)),
          // Caroline's own in-process fork of MCP/email (see
          // Caroline/backend/src/email/index.ts) -- action calls
          // (send/delete/move/mark/download) return immediately and
          // report their real outcome via a proactive follow-up instead
          // of blocking the turn on a slow IMAP/SMTP round trip.
          "caroline-email": createEmailTool((text) => this.injectProactive(text, false)),
          "caroline-appbrowser": createAppBrowserTool(),
          "caroline-ratatosk": createRatatoskTools(workspaceDir, (event) => this.send(event)),
          "caroline-consult": createConsultTools((event) => this.send(event)),
        };
        // notes_login asks the model to pass the user's email/password as
        // tool parameters -- directly against the hard rule (see login.ts's
        // ensure_squirrelwisdom_login, the squirrelwisdom-login skill) that
        // credentials only ever go through the native login window, never
        // through chat/tool-call context. Hidden here rather than patched
        // in MCP/notes since that binary is shared with the standalone
        // Notes MCP server, which has no such rule. Every other notes_*
        // tool is unaffected.
        const disallowedTools = ["mcp__caroline-notes__notes_login"];
        this.lastMcpServers = mcpServers;
        this.lastDisallowedTools = disallowedTools;

        // Defensive parallel-branch snapshot -- per explicit instruction
        // (2026-09-08, see the approved parallel-turns plan): forkSession()
        // is only safe against a file nothing else is writing to, and this
        // turn's OWN query() below is about to start appending to
        // resumeSessionId. This is the last moment a safe, static copy of
        // "right before this turn" can be taken -- submitOrBranch() forks
        // AGAIN off of THIS snapshot if a second message arrives while this
        // turn is running. The previous turn's snapshot is deleted first --
        // never kept longer than one turn, or these would accumulate one
        // full session copy per turn forever.
        if (this.preTurnSnapshotId) {
          const staleSnapshotId = this.preTurnSnapshotId;
          this.preTurnSnapshotId = null;
          void deleteSessionFile(workspaceDir, staleSnapshotId);
        }
        if (resumeSessionId) {
          try {
            const { sessionId: snapshotId } = await forkSession(resumeSessionId, { dir: workspaceDir });
            this.preTurnSnapshotId = snapshotId;
            console.error(`[caroline] [parallel] tab=${this.tabId} pre-turn snapshot ${resumeSessionId} -> ${snapshotId}`);
          } catch (err) {
            console.error(`[caroline] [parallel] tab=${this.tabId} pre-turn snapshot failed (parallel branching unavailable this turn):`, err);
          }
        }

        const options: Options = {
          ...(anthropicEnv ? { env: anthropicEnv } : {}),
          // Confirmed live (2026-09-08): the CLI's own automatic-compaction feature
          // fails outright under an env-overridden ANTHROPIC_API_KEY/BASE_URL (every
          // chatSource other than own-anthropic-oauth) -- it needs the real OAuth
          // ("firstParty") auth path specifically, not just A valid credential, so
          // the compaction call itself dies with "Not logged in -- Please run
          // /login" even though the main chat completion works fine through the
          // very same override. When compaction then can't shrink a big prompt, the
          // turn fails with "Prompt is too long" instead -- traced to every single
          // occurrence of that today, all on sw-proxy. Caroline already has its own
          // context-aging mechanism (compaction.ts) independent of this, so turning
          // the CLI's native one off for the sources where it's actually broken has
          // a real fallback, not a bare gap. own-anthropic-oauth is untouched --
          // native auto-compact isn't known broken there, so no reason to change it.
          ...(mode.chatSource !== "own-anthropic-oauth" ? { settings: { autoCompactEnabled: false } } : {}),
          // Diagnostic-only (2026-09-05): capture the underlying claude.exe
          // process's own stderr instead of only inferring failure from the
          // message stream going silent. Confirmed live that a resumed
          // session can fail to ever reach 'system'/'init' with NOTHING in
          // our own logs explaining why -- every restart just re-triggers
          // the watchdog's generic "stream ended unexpectedly" with no real
          // cause captured anywhere.
          stderr: (data: string) => {
            console.error(`[caroline] [claude-stderr] tab=${this.tabId} resume=${resumeSessionId ?? "(none)"}: ${data}`);
          },
          // 'auto' (a model classifier approving/denying each call) was
          // real friction for zero benefit here -- confirmed live, it
          // blocked a plain curl/PowerShell call the user explicitly
          // wanted run, with no way to point it at a narrower rule from
          // inside the session. Caroline is the user's own single-operator
          // autonomous tool, not a shared/multi-tenant surface, so there's
          // no one else's boundary for the classifier to protect -- explicit,
          // confirmed tradeoff: no gate at all now on any tool call.
          permissionMode: "bypassPermissions",
          allowDangerouslySkipPermissions: true,
          cwd: workspaceDir,
          mcpServers,
          disallowedTools,
          // Conversation persists across app restarts, not just within one
          // run -- but NOT via continue:true. continue always resumes "the
          // most recent session for this cwd", which is fine for a single
          // conversation but would make every tab race to resume the SAME
          // thread now that multiple tabs share one workspace/cwd (see
          // server.ts's `sessions` map). Each tab instead resumes its OWN
          // session id, captured from the SDK's own session_id (present on
          // every message) the first time this tab's query() ever runs and
          // persisted per-tab (durability.ts's tab-session-<id>.json). A
          // brand-new tab has no stored id yet -- omitting resume/continue
          // entirely just starts a fresh conversation, exactly once (except
          // the primary tab, which gets one migration fallback -- see
          // resolveResumeSessionId).
          ...(resumeSessionId ? { resume: resumeSessionId } : {}),
          // Read fresh at session start; a persona change from Settings
          // takes effect on the next session restart, not mid-session --
          // acceptable for now (see TODO.md), sessions restart often enough
          // via the watchdog/reconnect path.
          systemPrompt: {
            type: "preset",
            preset: "claude_code",
            append: [
              personaSystemPromptAppend(getPersona(workspaceDir)),
              vaultSecurityInstruction(),
              progressNarrationInstruction(),
              bashBackgroundInstruction(),
              timestampAwarenessInstruction(),
              noAlarmingInternalRecoveryInstruction(),
              noUpdateSentinelInstruction(),
              embeddedBrowserInstruction(),
              noFullFilesystemSearchInstruction(),
              recurringTasksInstruction(),
              preferWindowTargetedInputInstruction(),
              tableSizeGuidanceInstruction(),
              cheapImageDescriptionInstruction(),
              readContentNotHeadersInstruction(),
              markDiscussedEmailsReadInstruction(),
              checkSentMailTooInstruction(),
              closeWindowsAfterTaskInstruction(),
              preferCroppedScreenshotsInstruction(),
              consultLargeModelInstruction(),
              noRemoteFilesystemScansInstruction(),
              taskDecompositionInstruction(),
              scriptOrSubagentDelegationInstruction(),
              learnFromMistakesInstruction(),
            ].filter(Boolean).join("\n\n"),
          },
          // Lets Caroline discover/invoke skills seeded into her workspace's
          // Skills/ folder (see workspace.ts's seedSkills()) -- python-
          // environment, showing-files-in-chat, squirrelwisdom-login,
          // vault-backups, embedded-browser-troubleshooting today, more as
          // they're added. 'all' rather than an explicit list so a new skill
          // dropped into Skills/ (by a future code update, or the user/
          // Caroline herself) is available without an env/options change.
          skills: "all",
        };
        const cliPidBefore = await snapshotDirectChildPids(process.pid);
        const q = query({ prompt: this.inputStream(), options });
        this.activeQuery = q;
        // Fire-and-forget: the CLI process needs a moment to actually spawn
        // as a visible child before it shows up in a fresh snapshot -- see
        // processReaper.ts's own doc comment for why this pid matters (only
        // way to reap this exact instance later if it never exits on its own).
        setTimeout(async () => {
          const newPid = findNewPid(cliPidBefore, await snapshotDirectChildPids(process.pid));
          if (newPid !== null) {
            console.error(`[caroline] [reaper] tab=${this.tabId} identified this query()'s cli process pid=${newPid}`);
            this.cliProcessPid = newPid;
          }
        }, 3000);
        for await (const rawMessage of q) {
          this.lastActivity = Date.now();
          logSdkMessage(rawMessage);
          let message: SDKMessage = rawMessage;

          // See CLASSIFIER_REFUSAL_PATTERN's doc comment: the SDK synthesizes this
          // exact text in place of a real reply when Anthropic's API refuses a
          // request mid-turn on a content-classifier false positive. Detected here,
          // before the message ever reaches the chat UI.
          if (message.type === "assistant") {
            const refusalText = message.message.content
              .filter((b): b is Extract<typeof b, { type: "text" }> => b.type === "text")
              .map((b) => b.text)
              .find((t) => CLASSIFIER_REFUSAL_PATTERN.test(t));
            if (refusalText) {
              const category = extractClassifierRefusalCategory(refusalText);
              if (this.classifierRefusalRetryCount === 0) {
                this.classifierRefusalRetryCount += 1;
                console.error(`[caroline] classifier refusal (category=${category ?? "?"}) -- auto-retrying once, not shown to user`);
                this.pushMessage(
                  "[System note: your previous reply was blocked by an Anthropic API content-classifier " +
                  "false positive (unrelated to the actual conversation) and never reached the user. " +
                  "Please just try answering their last message again.]",
                );
                continue; // don't forward the raw error text to the chat UI
              }
              // Already retried once for this turn and it happened again -- stop
              // silently retrying (would risk looping forever) and tell the user
              // plainly instead of showing the raw SDK error text.
              console.error(`[caroline] classifier refusal recurred (category=${category ?? "?"}) after one retry -- telling the user`);
              const explanation =
                "Не смогла ответить на предыдущее сообщение: сработал внутренний фильтр безопасности Anthropic" +
                (category ? ` (категория «${category}»)` : "") +
                ", похоже на ложное срабатывание — с содержанием разговора это не связано. Повторная попытка тоже " +
                "не прошла. Попробуйте переформулировать сообщение или повторить чуть позже.";
              message = {
                ...message,
                message: { ...message.message, content: [{ type: "text", text: explanation, citations: null }] },
              };
              this.classifierRefusalRetryCount = 0;
            }
          }

          // SDKAssistantMessage.error === 'billing_error' is the SDK's own structured
          // signal for this (far more reliable than parsing text). Per explicit
          // instruction: API problems now retry automatically (see
          // API_RETRY_INTERVAL_MS/scheduleApiRetry) instead of just explaining and
          // giving up, AND never show as a chat bubble -- status-bar-only via
          // system_notice (see chat.js's system_notice handler and its red-lamp
          // treatment).
          if (message.type === "assistant" && message.error === "billing_error") {
            const source: "sw" | "anthropic" = mode.chatSource === "sw-proxy" ? "sw" : "anthropic";
            console.error(`[caroline] billing_error on chatSource=${mode.chatSource} -- source=${source}`);
            const { text: explanation, fellBackToSw } = await this.handleBalanceExhausted(source, mode.swLoggedIn);
            this.setConnState(fellBackToSw ? "limited" : "billing_blocked", explanation, { armIgnoreNextResult: true });
            this.scheduleApiRetry(`billing_error:${source}`, this.turnIsVoice);
            // This session's own subprocess still has the OLD (exhausted)
            // env baked in -- scheduleApiRetry's normal same-session retry
            // would just fail the same way again. Force it down so the next
            // while-loop iteration re-resolves mode and actually gets
            // sw-proxy (see restartForChatSourceSwitch's own doc comment).
            if (fellBackToSw) {
              this.restartForChatSourceSwitch = true;
              this.activeQuery?.close();
            }
            continue; // don't forward the raw/substituted message as a chat bubble
          }

          // See PROMPT_TOO_LONG_PATTERN's own doc comment. Checked before
          // CC_CLI_LIMIT_PATTERN below since this is a different failure
          // class entirely (a too-big resumed session, not a usage-window
          // cap) that needs its own handling, not just a status-bar message.
          if (message.type === "assistant") {
            const textBlocks = message.message.content
              .filter((b): b is Extract<typeof b, { type: "text" }> => b.type === "text")
              .map((b) => b.text);
            const promptTooLong = textBlocks.find((t) => PROMPT_TOO_LONG_PATTERN.test(t));
            if (promptTooLong) {
              console.error(`[caroline] [urgent-compaction] tab=${this.tabId} "Prompt is too long" detected, suppressing and triggering urgent compaction: ${truncateForLog(promptTooLong)}`);
              this.setConnState("restarting", "Urgent compaction...");
              const replayText = this.pendingUserText;
              const replayAttachments = this.pendingAttachments;
              console.error(`[caroline] [urgent-compaction] tab=${this.tabId} captured turn for replay: pendingUserText=${replayText !== null ? "set" : "null"} attachments=${replayAttachments.length}`);
              void this.runUrgentCompaction(replayText, replayAttachments);
              continue; // never forward "Prompt is too long" as a chat bubble
            }
            // Per explicit instruction: a STANDALONE occurrence of the same
            // underlying compaction-auth failure (see PROMPT_TOO_LONG_PATTERN's
            // doc comment) -- no "Prompt is too long" prefix this time, so no
            // urgent-compaction action, just suppress it. The account's real
            // login state is fine (claude auth status confirms it); this is
            // specifically the CLI's own internal compaction call complaining.
            const notLoggedIn = textBlocks.find((t) => NOT_LOGGED_IN_PATTERN.test(t));
            if (notLoggedIn) {
              console.error(`[caroline] [urgent-compaction] tab=${this.tabId} standalone "Not logged in" detected, suppressing (no action per explicit instruction): ${truncateForLog(notLoggedIn)}`);
              continue; // never forward as a chat bubble
            }
          }

          // Same treatment for Claude Code's own CLI subscription usage-cap message
          // (see CC_CLI_LIMIT_PATTERN's doc comment) -- distinct from billing_error
          // above: this resets on its own and there's nothing for the user to fix,
          // so it's yellow ("limited"), not red, and shows the actual CLI text
          // (e.g. "You've hit your session limit -- resets 12:20am") in the status
          // bar instead of a generic message.
          if (message.type === "assistant") {
            const limitText = message.message.content
              .filter((b): b is Extract<typeof b, { type: "text" }> => b.type === "text")
              .map((b) => b.text)
              .find((t) => CC_CLI_LIMIT_PATTERN.test(t));
            if (limitText) {
              // This is specifically the bundled CLI's own OAuth-subscription
              // usage-cap tracking (Pro/Max session/weekly limits) -- it has
              // no meaning for own-anthropic-key (pay-as-you-go, no such cap)
              // or sw-proxy (not the CLI's own subscription), so only
              // own-anthropic-oauth ever triggers this. No structured resetsAt
              // to hand markOwnAnthropicExhausted -- the human-readable time
              // is embedded in limitText itself, not reliably parseable, so
              // this uses the default cooldown like billing_error does.
              const fellBackToSw = mode.chatSource === "own-anthropic-oauth" && mode.swLoggedIn;
              if (fellBackToSw) markOwnAnthropicExhausted();
              const text = fellBackToSw
                ? `${limitText} Switching to SquirrelWisdom for now -- I'll switch back automatically.`
                : limitText;
              console.error(`[caroline] cc_cli_limit_message hit (fellBackToSw=${fellBackToSw}): ${truncateForLog(limitText)}`);
              this.setConnState("limited", text, { armIgnoreNextResult: true });
              this.scheduleApiRetry("cc_cli_limit_message", this.turnIsVoice);
              if (fellBackToSw) {
                this.restartForChatSourceSwitch = true;
                this.activeQuery?.close();
              }
              continue; // don't forward the raw text as a chat bubble
            }
          }

          // Structured signal, not text-matched -- see lastRateLimitInfo's
          // own doc comment for why this is tracked even when status isn't
          // (yet) 'rejected': a later silent stream-death needs to be able
          // to tell a limit hit apart from a real hang with no message at
          // all to go on.
          if (message.type === "rate_limit_event") {
            this.lastRateLimitInfo = message.rate_limit_info;
            if (message.rate_limit_info.status === "rejected") {
              this.ignoreNextResultRecovery = true;
              const fellBackToSw = this.handleRateLimitRejected("in-stream", message.rate_limit_info, mode.chatSource, mode.swLoggedIn);
              // Same reasoning as billing_error above: this session's env is
              // fixed, force it down so the fallback actually takes effect.
              if (fellBackToSw) {
                this.restartForChatSourceSwitch = true;
                this.activeQuery?.close();
              }
              continue; // don't forward as a chat bubble
            }
          }

          // The CLI's OWN internal retry, distinct from rate_limit_event --
          // "Emitted when an API request fails with a retryable error and
          // will be retried after a delay" (SDK's own doc comment). Tracked
          // for the same reason as lastRateLimitInfo: if the stream then
          // dies silently (our own watchdog interrupts a turn the CLI was
          // already patiently retrying on its own, or the CLI eventually
          // gives up after its own retries), the resulting failure must
          // still be recognized as limit-caused rather than a generic hang
          // that burns the restart budget -- per explicit instruction
          // (2026-09-05): a token/rate-limit shortage must NEVER surface as
          // the "giving up" fatal dialog, only ever as a quiet, infinite
          // retry.
          if (message.type === "system" && message.subtype === "api_retry") {
            console.error(`[caroline] api_retry: attempt=${message.attempt}/${message.max_retries} error=${message.error} delayMs=${message.retry_delay_ms}`);
            this.lastApiRetryError = message.error;
            continue; // internal CLI bookkeeping, never a chat bubble
          }

          // Capture this tab's own Claude session id the moment it's known,
          // so the NEXT restart/reconnect for this tab resumes it via
          // resume:<id> instead of starting a fresh conversation -- see this
          // tab's own options.resume above for why continue:true can't be
          // used once multiple tabs share one cwd.
          const sid = "session_id" in message && typeof message.session_id === "string" ? message.session_id : null;
          if (sid && sid !== this.lastSavedSessionId) {
            this.lastSavedSessionId = sid;
            saveTabSessionId(workspaceDir, this.tabId, sid);
          }
          if (message.type === "result") {
            this.turnPending = false;
            this.pendingUserText = null;
            this.pendingAttachments = [];
            clearPendingTurn(workspaceDir, this.tabId);
            this.classifierRefusalRetryCount = 0;
            this.lastApiRetryError = null;
            // Reaching a normal "result" (not suppressed via `continue` above) means
            // this turn -- whether the user's original one or a scheduled retry --
            // actually went through this time. Stop retrying.
            this.clearApiRetryTimer();
            // A plain "result" otherwise sends no caroline_status at all -- explicit
            // here so the frontend's lamp actually clears once recovery is real,
            // instead of staying stuck until the user happens to trigger some other
            // status event themselves (see connState's own doc comment).
            // ignoreNextResultRecovery guards the one case that ISN'T real
            // recovery: the very turn that just hit a limit/billing_error still
            // closes out with its own normal "result" (see that flag's own doc
            // comment) -- skip the reset there and wait for a later, genuine one.
            if (this.ignoreNextResultRecovery) {
              this.ignoreNextResultRecovery = false;
            } else if (this.connState.kind !== "connected") {
              this.setConnState("connected");
            }
            // The hourly tick landed while this turn (or the user's wider
            // multi-step task) was still active -- only actually run it once
            // hasLiveDialog agrees the user has gone quiet for a while, not
            // just because this one turn happened to finish (see
            // maybeCompact's own doc comment). Otherwise leave it pending
            // for the next result or the next hourly tick to re-check.
            if (this.pendingCompaction && !this.hasLiveDialog()) {
              this.pendingCompaction = false;
              void this.runCompaction();
            }
          }
          if (!this.silentTurn) {
            if (message.type === "result") {
              console.error(`[caroline] [transcript] result: isVoice=${this.turnIsVoice} silentTurn=${this.silentTurn}`);
              this.send({ type: "sdk_message", message, isVoice: this.turnIsVoice });
            } else {
              this.send({ type: "sdk_message", message });
            }
          }
          // Reset to true ("assume silent"), not false -- see silentTurn's doc comment.
          if (message.type === "result") {
            this.silentTurn = true;
            this.turnIsVoice = false;
            // Per explicit instruction (2026-09-08) -- see restartForDehydration's
            // own doc comment for why this HAS to be a real restart, not just an
            // in-place rewrite left for the live process to notice on its own.
            // Awaited before closing so the file is fully rewritten before the
            // fresh query() below ever tries to read it.
            await this.runDehydration(sid ?? this.lastSavedSessionId);
            this.restartForDehydration = true;
            this.activeQuery?.close();
          }
          if (message.type === "system" && message.subtype === "init") {
            this.hasSeenInit = true;
            console.error(`[caroline] runLoop: 'init' received ${Date.now() - queryStartedAt}ms after query() creation (tab=${this.tabId}, resume=${resumeSessionId ?? "(none)"})`);
            // Per explicit instruction (2026-09-06): connState previously only ever
            // flipped back to "connected" when a full turn completed (see the "result"
            // handler's own setConnState call, below) -- an idle tab that recovered from
            // restarting/restart_backoff (or just started fresh) had nothing to trigger
            // that until its NEXT turn finished, so the lamp could sit stale/yellow
            // indefinitely with nothing actually wrong. 'init' is the earliest proof this
            // query() is genuinely alive and talking to the CLI -- no reason to wait for
            // a full turn on top of that just to tell the user it's working. Not gated by
            // ignoreNextResultRecovery -- that flag exists to stop THIS SAME turn's own
            // trailing "result" from looking like a false recovery (see its own doc
            // comment); a brand new query() reaching init is a materially different,
            // more trustworthy signal, and if the account is genuinely still
            // limited/blocked the very next real API call still re-flips this correctly.
            if (this.connState.kind !== "connected") {
              this.setConnState("connected");
            }
            // Same "earliest proof this session is genuinely alive" trust level
            // as the connState recovery just above -- if own-Anthropic was
            // marked exhausted and THIS session (still resolved to it, or the
            // periodic recheck let it through again) reached init, treat that
            // as confirmation it's back. Wrong guesses self-heal exactly like
            // connState's: the very next real billing_error/rate_limit_event/
            // cc_cli_limit_message just re-blocks it via markOwnAnthropicExhausted.
            if (mode.chatSource === "own-anthropic-oauth" || mode.chatSource === "own-anthropic-key") {
              clearOwnAnthropicExhausted();
            }
            // checkHang below only catches an MCP server hanging mid-turn --
            // this catches the other half, one that failed to start at all
            // (bad binary path, crashed on launch, etc.). Previously this
            // threw, which tore down and restarted the WHOLE session via
            // handleFailure -- but a single unrelated server being down
            // (confirmed live: a machine-wide "playwright" server pointed at
            // a CDP endpoint that wasn't running) just re-fails the exact
            // same way on every restart, producing an infinite
            // restart-then-fail loop (visible in the UI as a permanently
            // "recovering session..." state) instead of Caroline simply
            // working normally without that one tool. A failed server here
            // behaves the same way it would in an interactive `claude`
            // session: it's unavailable, everything else still works.
            // 'needs-auth'/'pending'/'disabled' aren't logged here (an auth
            // prompt, a race that may still resolve, or intentional), only
            // 'failed' is.
            const failed = message.mcp_servers.filter((s) => s.status === "failed");
            for (const s of failed) this.scheduleMcpReconnect(q, s.name);
          }
        }
        // The generator ended on its own (not our doing) -- treat like a crash.
        if (!this.ended) {
          console.error(`[caroline] runLoop: generator ended without a result, ${Date.now() - queryStartedAt}ms after query() creation, hasSeenInit=${this.hasSeenInit} (tab=${this.tabId}, resume=${resumeSessionId ?? "(none)"})`);
          throw new Error("query() stream ended unexpectedly");
        }
      } catch (err) {
        if (this.ended) return;
        // This query() instance is being abandoned for a fresh one below --
        // per explicit instruction (2026-09-06), confirmed live that the old
        // CLI process can simply never exit on its own (found 11 such stuck
        // trees holding 240+ leaked node.exe processes between them). Watch
        // its pid (if identified -- see the setTimeout right after query()
        // was created) and force-kill its whole tree if it's still around
        // after a long grace period, instead of leaking forever.
        if (this.cliProcessPid !== null) {
          scheduleReapIfStale(this.cliProcessPid, REAP_GRACE_MS);
          this.cliProcessPid = null;
        }
        if (this.userStopRequested) {
          this.userStopRequested = false;
          this.turnPending = false;
          this.pendingUserText = null;
          this.pendingAttachments = [];
          this.send({ type: "caroline_status", status: "stopped" });
          // Caroline needs to know this happened, not just the UI -- an
          // interrupted tool call may have left something half-done (a
          // partially-sent email, a file half-written) that matters for
          // whatever she does next, and she shouldn't assume it completed.
          this.submit(
            "[The user just stopped what you were doing. Whatever action was in progress may be incomplete " +
              "or partially applied -- don't assume it finished. Wait for their next instruction.]",
            [],
            false,
          );
          continue; // fresh query() below, no failure logged/counted
        }
        // This session was deliberately closed above (in-stream) specifically
        // to pick up an own-Anthropic -> sw-proxy chat-source fallback -- see
        // restartForChatSourceSwitch's own doc comment for why a live session
        // can't just apply that switch to itself. Checked before any other
        // classification below so this expected, deliberate restart is never
        // misattributed to a generic hang/crash or miscounted against the
        // restart budget, regardless of what the resulting thrown error (or
        // clean generator end -> synthesized "stream ended unexpectedly"
        // above) happens to look like.
        if (this.restartForChatSourceSwitch) {
          this.restartForChatSourceSwitch = false;
          console.error("[caroline] runLoop catch: session closed for a chat-source fallback -- fresh query() will pick up sw-proxy");
          // Confirmed live (2026-09-08): lastRateLimitInfo's own doc comment says it
          // deliberately stays 'rejected' until a later event reports otherwise -- true
          // and correct when there's only ever ONE possible chat source (the original
          // design), but once a source switch is possible, that stale info survives
          // into the NEW session on the NEW source and misattributes its own, unrelated
          // failures to "still exhausted" instead of surfacing what actually went wrong.
          // Confirmed live: an sw-proxy request genuinely failed with "Prompt is too
          // long" (nothing to do with rate limits), and the very next silent stream
          // death got misclassified as still-rate-limited purely because this field was
          // never cleared -- the fallback itself (ownAnthropicBlockedUntil in
          // subscriptionMode.ts) is unaffected by this and stayed correct throughout,
          // but the UI/logs lied about why. A session on a fresh chat source starts
          // clean; whatever this source's own real failures turn out to be will set
          // this again on their own merits.
          this.lastRateLimitInfo = null;
          // Same "don't replay raw text, let scheduleApiRetry's own generic
          // nudge (already scheduled by the caller that closed this session)
          // pick things back up" treatment as the billing/rate-limit catch
          // paths below -- this IS that same kind of recoverable failure,
          // just detected in-stream instead of via a thrown/matched error.
          this.turnPending = false;
          this.pendingUserText = null;
          this.pendingAttachments = [];
          clearPendingTurn(workspaceDir, this.tabId);
          continue; // fresh query() below, no restart-budget cost, no replay
        }
        // This session was deliberately closed above (in-stream) by
        // runUrgentCompaction() -- see PROMPT_TOO_LONG_PATTERN's own doc
        // comment. Checked right alongside restartForChatSourceSwitch, same
        // reasoning: this expected, deliberate restart must never be
        // misattributed to a generic hang/crash or cost restart budget.
        // Unlike that path, THIS one DOES replay -- the turn that hit
        // "Prompt is too long" never got a real answer, so the user's actual
        // question would just be silently dropped otherwise.
        if (this.restartForUrgentCompaction) {
          this.restartForUrgentCompaction = false;
          const replayText = this.urgentCompactionReplayText;
          const replayAttachments = this.urgentCompactionReplayAttachments;
          this.urgentCompactionReplayText = null;
          this.urgentCompactionReplayAttachments = [];
          console.error(`[caroline] [urgent-compaction] runLoop catch: tab=${this.tabId} session closed for urgent compaction -- fresh query() will resume the compacted session, replayText=${replayText !== null ? "set" : "null"}`);
          this.turnPending = false;
          this.pendingUserText = null;
          this.pendingAttachments = [];
          clearPendingTurn(workspaceDir, this.tabId);
          if (replayText !== null) {
            console.error(`[caroline] [urgent-compaction] tab=${this.tabId} replaying the turn that hit "Prompt is too long": ${truncateForLog(replayText)}`);
            this.submit(replayText, replayAttachments, true, false, this.turnIsVoice);
          } else {
            console.error(`[caroline] [urgent-compaction] tab=${this.tabId} no captured turn to replay (pendingUserText was already null) -- nothing queued`);
          }
          continue; // fresh query() below, no restart-budget cost
        }
        // This session was deliberately closed above (in-stream) right after
        // its own normal "result" -- see restartForDehydration's own doc
        // comment. Unlike urgentCompaction, no replay: the turn already
        // completed and was already delivered to the user, nothing was lost.
        if (this.restartForDehydration) {
          this.restartForDehydration = false;
          console.error(`[caroline] dehydrate: tab ${this.tabId} session closed for post-turn dehydration -- fresh query() will resume the rewritten transcript`);
          continue; // fresh query() below, no restart-budget cost, no replay
        }
        // Fallback path for handleBalanceExhausted: covers a billing failure that
        // surfaced as a thrown error instead of an in-stream assistant message
        // with .error === 'billing_error' (the primary detection point, above in
        // the for-await loop) -- e.g. if query() itself rejects before yielding
        // anything. Text-matched here (not the structured field) since a thrown
        // JS Error has no such field; source is inferred from which balance the
        // message text names, not from chatSource (the `mode` local from the
        // top of this iteration is out of scope in a catch block -- re-resolved
        // fresh below instead, cheap and deterministic given nothing else about
        // login/settings state changed between there and here).
        const balanceSource = detectBalanceExhaustion(String(err));
        if (balanceSource) {
          console.error(`[caroline] runLoop catch: billing failure (source=${balanceSource}) thrown instead of in-stream -- not restarting/replaying`);
          const { swLoggedIn: recentSwLoggedIn } = await resolveMode(workspaceDir);
          const { text: explanation, fellBackToSw } = await this.handleBalanceExhausted(balanceSource, recentSwLoggedIn);
          this.turnPending = false;
          this.pendingUserText = null;
          this.pendingAttachments = [];
          clearPendingTurn(workspaceDir, this.tabId);
          this.setConnState(fellBackToSw ? "limited" : "billing_blocked", explanation);
          this.scheduleApiRetry(`billing_error(thrown):${balanceSource}`, this.turnIsVoice);
          continue; // fresh query() below, but nothing doomed gets replayed into it
        }
        // Same idea as the lastRateLimitInfo check just below, for the
        // CLI's own api_retry signal instead: if the CLI told us moments
        // ago it was already retrying a rate_limit error internally, and
        // the stream then dies (our watchdog interrupted it mid-retry, or
        // the CLI itself gave up after exhausting its own retries), this is
        // still a limit-caused failure, not a generic one -- per explicit
        // instruction (2026-09-05), a token/rate-limit shortage must never
        // reach the restart-budget "giving up" dialog.
        if (this.lastApiRetryError === "rate_limit") {
          console.error("[caroline] runLoop catch: stream died with lastApiRetryError='rate_limit' -- treating as a limit hit, not a generic failure");
          this.turnPending = false;
          clearPendingTurn(workspaceDir, this.tabId);
          this.setConnState("limited", "Hit the Claude usage limit. Retrying automatically.");
          this.scheduleApiRetry("api_retry:rate_limit", this.turnIsVoice);
          continue; // fresh query() below, no restart-budget cost, no replay
        }
        // A silent stream death (no billing_error, no cc_cli_limit_message
        // text, nothing) with the most recently known rate limit status
        // already 'rejected' -- treat it as the SAME limit hit continuing,
        // not a fresh generic failure. See lastRateLimitInfo's own doc
        // comment for the confirmed live incident this covers: a hard
        // usage cutoff killed the stream with no text to match at all.
        if (this.lastRateLimitInfo?.status === "rejected") {
          console.error("[caroline] runLoop catch: stream died with lastRateLimitInfo still 'rejected' -- treating as a limit hit, not a generic failure");
          this.turnPending = false;
          clearPendingTurn(workspaceDir, this.tabId);
          // `mode` from the top of this iteration is out of scope in a catch
          // block -- re-resolved fresh here, same reasoning as balanceSource above.
          const recentMode = await resolveMode(workspaceDir);
          this.handleRateLimitRejected("silent-stream-death", this.lastRateLimitInfo, recentMode.chatSource, recentMode.swLoggedIn);
          continue; // fresh query() below, no restart-budget cost, no replay
        }
        await this.handleFailure(err);
      }
    }
  }

  private checkHang(): void {
    // Unconditional heartbeat, every WATCHDOG_INTERVAL_MS (5s), whether or
    // not anything is wrong -- proves this timer itself is still alive and
    // shows exactly what it saw at any given moment, instead of only ever
    // logging when it decides to act. Deliberately verbose per explicit
    // request: better a noisy log than another "I can't tell what happened".
    const effectiveTimeoutMs = this.hasSeenInit ? HANG_TIMEOUT_MS : STARTUP_TIMEOUT_MS;
    console.error(`[caroline] checkHang tick: turnPending=${this.turnPending} lastActivityMs=${Date.now() - this.lastActivity} hangCount=${this.hangCount} hangInterruptedAt=${this.hangInterruptedAt} hasSeenInit=${this.hasSeenInit} effectiveTimeoutMs=${effectiveTimeoutMs}`);
    // turnPending alone used to gate this whole function -- confirmed live
    // (2026-09-05) as a real, silent gap: a non-primary tab (no auto-greeting
    // to force a turn right after restart, unlike the primary tab) whose
    // query() creation itself got stuck before ever reaching 'system'/'init'
    // sat with turnPending permanently false, so this function returned
    // immediately on every single tick, forever, with NO escalation ever
    // triggered -- confirmed stuck for 500+ seconds (past its own 300s
    // STARTUP_TIMEOUT_MS) with zero self-healing, invisible to every restart
    // mechanism that depends on this watchdog. A stuck cold-start is exactly
    // as real a hang as a stuck mid-turn; only skip the check for a session
    // that's genuinely idle AFTER a normal startup (hasSeenInit true).
    if (!this.turnPending && this.hasSeenInit) {
      this.hangInterruptedAt = null; // legitimately idle -- clear any stale escalation state
      return;
    }
    // Still cold-starting MCP servers (hasn't reached 'system'/'init' yet)
    // gets STARTUP_TIMEOUT_MS instead of HANG_TIMEOUT_MS -- see hasSeenInit's
    // doc comment. Once init has been seen, the normal, tighter mid-turn
    // timeout applies for the rest of this session's life.
    if (Date.now() - this.lastActivity < effectiveTimeoutMs) return;

    if (this.hangInterruptedAt === null) {
      this.hangCount++;

      if (this.hangCount >= 2) {
        // This exact session already hung once before and "recovered" (see
        // hangCount's doc comment) -- confirmed live that interrupt()
        // clearing turnPending does NOT mean whatever actually caused the
        // hang got fixed, since it's usually a wedged MCP connection that
        // persists across turns within the same session. Skip the soft path
        // entirely this time and force a full teardown+restart immediately,
        // so the conversation can keep going on a session with fresh MCP
        // connections instead of repeating the same futile interrupt dance
        // for hours (confirmed live: 8 hangs over 3.5 hours, never once
        // escalating, before the whole process eventually froze solid).
        console.error(`[caroline] session hung again (hang #${this.hangCount} this session) -- forcing close() immediately, skipping interrupt`);
        try {
          this.activeQuery?.close();
        } catch (err) {
          console.error("[caroline] close() threw while forcing a repeatedly-hung session down:", err);
        }
        return;
      }

      // First hang for this session -- try the soft path first.
      console.error("[caroline] session appears hung (no activity while a turn was pending); interrupting");
      this.hangInterruptedAt = Date.now();
      this.activeQuery?.interrupt().catch((err) => console.error("[caroline] interrupt() during soft hang-recovery failed (hang detector will escalate to a hard close() if this hang recurs):", err));
      // runLoop's for-await will throw/end once interrupted; handleFailure runs from there.
      return;
    }

    // Confirmed live as a real failure mode: interrupt() sends a control
    // message the CLI has to still be responsive enough to *receive and act
    // on* -- if the underlying transport is genuinely wedged (not just the
    // turn's own work being slow), interrupt() silently does nothing and
    // this function kept re-firing every WATCHDOG_INTERVAL_MS forever with
    // no escalation, since it never tracked whether a previous attempt had
    // already been made. Confirmed against a real stuck session that sat
    // for 900+ seconds past HANG_TIMEOUT_MS with interrupt() retried every
    // 5s and never taking effect. Give the soft interrupt one grace window,
    // then force it: close() tears down the transport directly and doesn't
    // depend on the other side cooperating ("after calling close(), no
    // further messages will be received" -- confirmed in the SDK's own
    // Query type docs), which reliably ends the for-await loop and lets
    // handleFailure's normal restart path take over.
    if (Date.now() - this.hangInterruptedAt < HANG_ESCALATION_GRACE_MS) return;
    console.error("[caroline] interrupt() didn't unstick the session within the grace window; forcing close()");
    this.hangInterruptedAt = null;
    try {
      this.activeQuery?.close();
      // close() ending the for-await loop is what drives recovery from here --
      // runLoop's own catch block (the same path every other crash already
      // goes through) calls handleFailure once the loop actually throws/ends.
      // Not calling handleFailure directly here on purpose: doing so
      // unconditionally would double-fire it (and double-count toward
      // MAX_RESTARTS_PER_WINDOW, and risk starting two overlapping sessions)
      // on the expected case where close() works exactly as documented.
    } catch (err) {
      console.error("[caroline] close() threw while forcing a hung session down:", err);
    }
  }

  private async handleFailure(err: unknown): Promise<void> {
    console.error(`[caroline] handleFailure: entered. hangCount=${this.hangCount} turnPending=${this.turnPending} pendingUserText=${this.pendingUserText !== null ? "set" : "null"} descendantProcesses=${await describeDescendantProcesses(process.pid)}`);
    console.error("[caroline] session failure, restarting:", err);
    if (err instanceof Error && err.stack) {
      console.error("[caroline] handleFailure: error stack:", err.stack);
    }
    // Any pending per-server reconnect retries belong to the query instance
    // that's being torn down -- scheduleMcpReconnect's own activeQuery check
    // would catch this anyway, but clearing here avoids leaking timers.
    this.clearMcpReconnectTimers();
    const now = Date.now();
    this.restartTimestamps = this.restartTimestamps.filter((t) => now - t < RESTART_WINDOW_MS);
    this.restartTimestamps.push(now);
    console.error(`[caroline] handleFailure: restartTimestamps now has ${this.restartTimestamps.length}/${MAX_RESTARTS_PER_WINDOW} entries within the ${RESTART_WINDOW_MS / 60_000}min window`);
    if (this.restartTimestamps.length > MAX_RESTARTS_PER_WINDOW) {
      console.error(`[caroline] handleFailure: restart budget exceeded (${this.restartTimestamps.length}/${MAX_RESTARTS_PER_WINDOW} in ${RESTART_WINDOW_MS / 60_000}min) -- backing off ${RESTART_BACKOFF_MS}ms (flat, not exponential) instead of giving up`);
      this.setConnState(
        "restart_backoff",
        `Trouble reconnecting (failed ${this.restartTimestamps.length} times in ${RESTART_WINDOW_MS / 60_000}min) -- retrying in ${Math.round(RESTART_BACKOFF_MS / 1000)}s`,
      );
      // hasLiveDialog() now reads restart_backoff as "not live" (see its own doc
      // comment) -- check right away instead of waiting for the next hourly tick,
      // so a session that's actually stuck (as opposed to just genuinely busy)
      // gets a chance to shrink/reset itself as soon as that's known, not up to an
      // hour later. No-op if there's nothing to compact yet or a run is already
      // in progress (see runCompaction's own guards).
      this.maybeCompact();
      await sleep(RESTART_BACKOFF_MS);
    }
    console.error(`[caroline] handleFailure: sending caroline_status=restarting, will loop back into runLoop's while() for a fresh query()`);
    this.setConnState("restarting", String(err));
    // Per explicit instruction (2026-09-08): every real (non-deliberate)
    // restart -- reaching handleFailure at all already means this, since
    // every EXPECTED restart (dehydration, chat-source switch, urgent
    // compaction) bypasses handleFailure entirely via its own `continue` in
    // runLoop's catch block -- must be visible to Caroline herself, so she
    // has the real context if the user later asks "what happened just now".
    // Framed explicitly as internal/not-alarming: the user has their own
    // way to notice a restart (status bar), this is for HER situational
    // awareness only. Covers both this session's own internal checkHang
    // escalation AND an external watchdog force-killing this tab's CLI
    // process -- both end up here the same way (the query() stream ending),
    // so one note covers both without needing separate plumbing.
    const watchdogNote =
      `[System note: this session just recovered from an internal failure (hangCount=${this.hangCount}): ` +
      `${err instanceof Error ? err.message : String(err)}. This is Caroline's own infrastructure ` +
      `self-healing, already handled -- for your own situational awareness only. Do not mention this or ` +
      `sound any alarm about it to the user unless they specifically ask what happened just now.]`;
    if (this.pendingUserText !== null) {
      console.error(`[caroline] handleFailure: replaying pendingUserText (len=${this.pendingUserText.length}) into the fresh session`);
      this.pushMessage(`${watchdogNote}\n\n${this.pendingUserText}`, this.pendingAttachments);
    } else {
      // Nothing specific was left hanging, but the previous session could
      // have died mid-tool-call with something genuinely unfinished (a
      // half-sent email, a file half-written, an in-progress upload) that
      // wouldn't show up as a pendingUserText at all. Nudge her to check
      // and either continue or use the [[NO_UPDATE]] sentinel (see
      // policies.ts's noUpdateSentinelInstruction) so a "nothing to do"
      // outcome doesn't clutter the chat with a filler reply. Phrased in
      // whichever language the conversation was actually in -- an English
      // instruction landing after a Russian conversation (or vice versa)
      // risked knocking her into replying in the wrong language, confirmed
      // as a real (if minor) annoyance, not just a theoretical one.
      const lang = await detectRecentLanguage();
      // Re-check -- detectRecentLanguage() is async and confirmed live
      // (2026-09-04) to take several seconds; a real user message can
      // arrive and get queued (via submit(), setting pendingUserText)
      // during that gap. Injecting this nudge on top of it lands both in
      // the same turn, and the model reliably obeys the nudge's strict
      // "reply exactly [[NO_UPDATE]]" instruction over answering the real
      // question -- confirmed live: a genuine user message got silently
      // swallowed this way, with only "[[NO_UPDATE]]" (itself suppressed
      // from the UI) coming back. If a real message showed up in the
      // meantime, let it get its own normal turn instead.
      if (this.pendingUserText !== null) {
        // The real message that just appeared will get its own normal
        // submit() turn (not through here), which never sees watchdogNote --
        // queue it separately so the context still isn't lost, same
        // reasoning as the branch above.
        console.error("[caroline] handleFailure: pendingUserText appeared during language detection -- skipping the continue-or-silent nudge, injecting watchdogNote on its own instead");
        this.injectProactive(watchdogNote, true);
      } else {
        console.error(`[caroline] handleFailure: no pendingUserText -- injecting continue-or-silent nudge (lang=${lang})`);
        this.injectProactive(`${watchdogNote}\n\n${CONTINUE_OR_SILENT_NUDGE[lang]}`, false);
      }
    }
    console.error("[caroline] handleFailure: done, returning to runLoop");
  }

  /**
   * Handles a chat turn failing because the money behind it ran out --
   * either SquirrelWisdom's own wallet (chatSource "sw-proxy", proxied
   * through Camerlengo's Api2AnthropicProxy.py, which returns HTTP 402
   * insufficient_balance once the wallet drops below
   * Config.ANTHROPIC_PROXY_MIN_BALANCE_PIA) or the user's own direct
   * Anthropic account (chatSource "own-anthropic-oauth"/"own-anthropic-key",
   * Anthropic's own "credit balance is too low" response). Unlike a
   * transient classifier refusal (see CLASSIFIER_REFUSAL_PATTERN), retrying
   * the SAME request against the SAME source accomplishes nothing here -- so
   * for the sw-proxy case this never auto-recovers on its own: it explains
   * what happened and opens the top-up checkout window directly (same flow
   * as Settings' "open_payment_from_settings"), so paying takes one click
   * instead of hunting through Settings first. For the anthropic case, if
   * the user also has a logged-in SquirrelWisdom account, it falls back to
   * that instead (see subscriptionMode.ts's markOwnAnthropicExhausted) --
   * per explicit instruction (2026-09-08): a paid, logged-in SW account must
   * not just sit unused while every turn fails because the user's own
   * Anthropic credits ran dry. `swLoggedIn` is the SAME mode.swLoggedIn the
   * caller already resolved for this turn.
   * Returns the explanation text plus whether it fell back (caller decides
   * where the text ends up -- a synthesized assistant message when caught
   * in-stream, or injectProactive when caught as a thrown error in runLoop's
   * catch block -- and which connState kind fits: still-blocked is red,
   * fell-back-automatically reads better as the same yellow "limited" a
   * rate-limit gets).
   */
  private async handleBalanceExhausted(source: "sw" | "anthropic", swLoggedIn: boolean): Promise<{ text: string; fellBackToSw: boolean }> {
    let paymentOpened = false;
    let fellBackToSw = false;
    if (source === "sw") {
      try {
        const checkoutUrl = await createTopupCheckoutUrl();
        this.send({ type: "open_payment", requestId: randomUUID(), checkoutUrl });
        paymentOpened = true;
      } catch (err) {
        console.error("[caroline] handleBalanceExhausted: createTopupCheckoutUrl failed:", err);
      }
    } else if (source === "anthropic" && swLoggedIn) {
      // No resetsAt here -- Anthropic's billing_error carries no reset time
      // (unlike a rate limit), so this uses markOwnAnthropicExhausted's
      // default cooldown and re-checks own-Anthropic periodically.
      markOwnAnthropicExhausted();
      fellBackToSw = true;
    }
    console.error(`[caroline] handleBalanceExhausted: source=${source} paymentOpened=${paymentOpened} fellBackToSw=${fellBackToSw}`);
    return { text: BALANCE_EXHAUSTED_MESSAGE[source](paymentOpened, fellBackToSw), fellBackToSw };
  }
}

type NudgeLanguage = "russian" | "english";

/**
 * Real language detection via Camerlengo's ai:detectLanguage (an actual
 * LLM call, see reforce's AI.py detectLanguage) -- deliberately NOT a
 * Cyrillic/Latin heuristic, per explicit instruction: language detection
 * must go through the real API everywhere in this codebase. Falls back to
 * "english" whenever the API can't tell (no recent text, the call fails,
 * or it genuinely doesn't recognize the sample) -- also per explicit
 * instruction. This project only ever actually switches between Russian
 * and English in its own instruction phrasing (CONTINUE_OR_SILENT_NUDGE,
 * STARTUP_GREETING_NUDGE below), so the real detected ISO code is
 * collapsed to that binary here; a wrong guess just means one proactive
 * message reads a little oddly, not a functional failure.
 */
async function detectRecentLanguage(): Promise<NudgeLanguage> {
  try {
    const entries = readRecentHistory(workspaceDir, 5);
    const lastText = entries.length > 0 ? entries[entries.length - 1].text : "";
    if (!lastText.trim()) return "english";
    // detectLanguage() has its own internal 15s ceiling (see voice.ts's
    // callApi), which is fine for a real voice/STT pipeline but far too
    // generous here -- confirmed live (2026-09-05) it took ~20s for a
    // restarting session's recovery nudge to actually go out, delaying the
    // one thing (telling Caroline to pick back up) that's supposed to
    // happen as soon as possible after a restart. Getting the wrong
    // language guess (falls back to "english") is a minor cosmetic
    // annoyance; a slow recovery nudge is the actual problem this exists to
    // avoid, so race it against a much shorter local timeout instead of
    // waiting out the full 15s.
    const iso = await Promise.race([
      detectLanguage(lastText),
      new Promise<null>((resolve) => setTimeout(() => resolve(null), 3_000)),
    ]);
    return iso === "ru" ? "russian" : "english";
  } catch (err) {
    console.error("[caroline] detectLanguage race failed, defaulting to english:", err);
    return "english";
  }
}

const CONTINUE_OR_SILENT_NUDGE: Record<NudgeLanguage, string> = {
  russian: "Продолжи неоконченную работу, если она есть. Если нет — ничего не делай и ответь ровно [[NO_UPDATE]], без пояснений.",
  english: "Continue any unfinished work, if there is any. If not, do nothing and reply with exactly [[NO_UPDATE]], with no explanation.",
};

/**
 * Static (never routed through the model -- see handleBalanceExhausted's doc
 * comment for why) explanation shown when a chat turn fails on a depleted
 * balance. "sw" = SquirrelWisdom wallet (paymentOpened tells whether the
 * top-up checkout window actually opened); "anthropic" = the user's own
 * direct Anthropic account, which Caroline has no self-serve top-up link
 * for -- points them at Anthropic's own console instead.
 *
 * Always English, regardless of conversation language -- this is
 * status-bar/system_notice UI chrome (see setConnState's "billing_blocked"
 * case), not a chat reply, and the app's UI is English-only. Confirmed
 * live (2026-09-05) that this (and a separate rate-limit message) had been
 * phrased in Russian when the recent conversation was in Russian, which is
 * correct for CONTINUE_OR_SILENT_NUDGE/STARTUP_GREETING_NUDGE (real chat
 * content) but wrong here.
 */
const BALANCE_EXHAUSTED_MESSAGE: Record<"sw" | "anthropic", (paymentOpened: boolean, fellBackToSw: boolean) => string> = {
  sw: (paymentOpened) =>
    "Couldn't reply -- the SquirrelWisdom wallet balance ran out." +
    (paymentOpened
      ? " I opened the top-up checkout window -- you can pay right now and I'll answer this message once it clears."
      : " I couldn't open the payment window automatically -- please top up via Settings -> Account & Billing."),
  anthropic: (_paymentOpened, fellBackToSw) =>
    fellBackToSw
      ? "Your own Anthropic account is out of credits -- switching to your SquirrelWisdom account for now. " +
        "I'll switch back automatically once Anthropic is available again (or you can top up sooner at " +
        "console.anthropic.com's Billing section)."
      : "Couldn't reply -- your own Anthropic account is out of credits. I can't top that up myself (it's not " +
        "through SquirrelWisdom) -- please visit console.anthropic.com's Billing section.",
};

/**
 * Fires once per backend-process lifetime (see `hasGreeted` at the call
 * site), the moment the primary tab's very first turn is about to run --
 * per explicit instruction, Caroline must never come back up silently:
 * every fresh launch or restart she should proactively say she's back and
 * ready, in character (persona.ts's system prompt already carries her
 * personality/gender-agreement rules -- this only supplies the language and
 * the occasion, not scripted wording).
 */
const STARTUP_GREETING_NUDGE: Record<NudgeLanguage, string> = {
  russian: "Ты только что запустилась (или перезапустилась). Поприветствуй пользователя проактивно, в своём " +
    "обычном стиле и с учётом своей личности — дай понять, что ты снова на связи и готова к работе. Коротко, " +
    "без лишних пояснений о том, что это стартовое сообщение.",
  english: "You just started up (or restarted). Proactively greet the user in your own voice and personality -- " +
    "let them know you're back online and ready to work. Keep it brief, and don't explain that this is a " +
    "startup message.",
};

/**
 * Handles the Settings-screen operations (login, MCP server add/remove) by
 * shelling out to the same bundled claude.exe the chat sessions use -- no
 * separate auth or config storage of Caroline's own.
 */
async function handleControlRequest(
  parsed: {
    op?: string; name?: string; command?: string; args?: string[];
    persona?: Partial<Persona> & { profileKey?: "custom" | "caroline" | "peter"; photosDir?: string };
    profileKey?: "caroline" | "peter";
    audioBase64?: string; format?: string; text?: string; path?: string;
    requestId?: string; outcome?: "saved" | "cancelled" | "closed" | "error"; message?: string;
    email?: string; password?: string; cancelled?: boolean; isRegister?: boolean;
    anthropicApiKey?: string;
    smtp2goApiKey?: string;
    smtp2goSender?: string;
    enabled?: boolean;
    filePath?: string;
  },
  send: (event: OutEvent) => void,
  // The session tied to whichever connection actually sent this request --
  // undefined for the tab-agnostic HTTP /api/control path, which falls back
  // to the primary tab (see call sites below). Ops below that are
  // inherently per-tab (force_restart, editor/login nudges) act on this
  // one; shutdown_sync is a deliberate exception (see its own case).
  session: ChatSession | undefined,
): Promise<void> {
  const op = parsed.op;
  console.error(`[caroline] [control] op=${op ?? "unknown"} received`);
  try {
    switch (op) {
      case "auth_status": {
        const r = await authStatus(workspaceDir);
        console.error(`[caroline] [control:auth_status] ok=${r.code === 0}`);
        send({ type: "control_response", op, ok: r.code === 0, stdout: r.stdout, stderr: r.stderr });
        break;
      }
      case "auth_login": {
        console.error(`[caroline] [control:auth_login] spawning auth login flow`);
        const proc = spawnAuthLogin(workspaceDir);
        proc.stdout?.on("data", (d) => send({ type: "control_stream", op, chunk: d.toString() }));
        proc.stderr?.on("data", (d) => send({ type: "control_stream", op, chunk: d.toString() }));
        proc.on("close", (code) => {
          console.error(`[caroline] [control:auth_login] closed code=${code}`);
          send({ type: "control_response", op, ok: code === 0 });
        });
        break;
      }
      case "auth_logout": {
        const r = await authLogout(workspaceDir);
        console.error(`[caroline] [control:auth_logout] ok=${r.code === 0}`);
        send({ type: "control_response", op, ok: r.code === 0, stdout: r.stdout, stderr: r.stderr });
        break;
      }
      case "force_restart": {
        // Manual escape hatch for exactly the failure mode that motivated
        // hangCount's fast-path escalation and BackendHealthWatchdog: lets
        // a human (or a script hitting POST /api/control) force this
        // session down and get a fresh one without touching OS processes.
        console.error(`[caroline] [control:force_restart] forcing session restart, hadSession=${!!session}`);
        session?.forceRestart();
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "mode_get": {
        // Backs Settings' "Account & Billing" resolved-chat-source line
        // (Part 6) -- one round trip covering own-Anthropic vs SW-proxy vs
        // none, same resolution runLoop() uses to build query()'s env.
        const mode = await resolveMode(workspaceDir);
        console.error(`[caroline] [control:mode_get] mode=${JSON.stringify(mode)}`);
        send({ type: "control_response", op, ok: true, stdout: JSON.stringify(mode) });
        break;
      }
      case "sw_status": {
        const status = await getSwStatus();
        console.error(`[caroline] [control:sw_status] status=${JSON.stringify(status)}`);
        send({ type: "control_response", op, ok: true, stdout: JSON.stringify(status) });
        break;
      }
      case "sw_logout": {
        // Added for testing subscription-mode combinations (there was
        // previously no way to exercise the "SW not logged in" branch
        // without hand-deleting the credentials file) -- see login.ts's
        // clearCredentials doc comment for why this also logs Notes out.
        console.error(`[caroline] [control:sw_logout] clearing SquirrelWisdom credentials`);
        clearCredentials();
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "own_anthropic_key_get": {
        const key = getOwnAnthropicApiKey(workspaceDir);
        // Only ever reveal whether one is set, never the value itself, once
        // saved -- same reasoning as never echoing a password back.
        console.error(`[caroline] [control:own_anthropic_key_get] isSet=${!!key}`);
        send({ type: "control_response", op, ok: true, stdout: JSON.stringify({ isSet: !!key }) });
        break;
      }
      case "own_anthropic_key_set": {
        console.error(`[caroline] [control:own_anthropic_key_set] clearing=${!parsed.anthropicApiKey}`);
        setOwnAnthropicApiKey(workspaceDir, parsed.anthropicApiKey ?? null);
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "sms_account_get": {
        const status = await getSmsAccountStatus();
        console.error(`[caroline] [control:sms_account_get] hasAccount=${status.hasAccount} error=${status.error ?? "none"}`);
        send({ type: "control_response", op, ok: true, stdout: JSON.stringify(status) });
        break;
      }
      case "sms_account_set": {
        if (!parsed.smtp2goApiKey) throw new Error("sms_account_set requires smtp2goApiKey");
        const result = await setSmsAccount(parsed.smtp2goApiKey, parsed.smtp2goSender ?? null);
        console.error(`[caroline] [control:sms_account_set] ok=${result.ok}`);
        send({ type: "control_response", op, ok: result.ok, stderr: result.error });
        break;
      }
      case "sms_account_remove": {
        const result = await removeSmsAccount();
        console.error(`[caroline] [control:sms_account_remove] ok=${result.ok}`);
        send({ type: "control_response", op, ok: result.ok, stderr: result.error });
        break;
      }
      case "ratatosk_status_get": {
        // Backs Settings' Ratatosk section -- both identities in one round
        // trip, same reasoning as ratatosk_identity_status's chat-tool twin.
        const ownerEmail = isLoggedIn() ? loggedInEmail() : null;
        const carolineEmail = hasOwnRatatoskAccount(workspaceDir) ? ownRatatoskEmail(workspaceDir) : null;
        console.error(`[caroline] [control:ratatosk_status_get] ownerEmail=${ownerEmail ?? "null"} carolineEmail=${carolineEmail ?? "null"}`);
        send({
          type: "control_response", op, ok: true,
          stdout: JSON.stringify({ ownerEmail, carolineEmail }),
        });
        break;
      }
      case "ratatosk_channel_status": {
        // Diagnostic-only: the headless owner-DM poll loop's own internal
        // state (tick count, cached groupId, cursor, last error) -- lets a
        // human (or a script over /api/control) see what it's actually
        // doing without needing to grep caroline.log by hand.
        const status = getRatatoskChannelStatus();
        console.error(`[caroline] [control:ratatosk_channel_status] status=${JSON.stringify(status)}`);
        send({ type: "control_response", op, ok: true, stdout: JSON.stringify(status) });
        break;
      }
      case "ratatosk_own_account_register": {
        console.error(`[caroline] [control:ratatosk_own_account_register] registering own Ratatosk account`);
        const result = await ensureOwnRatatoskAccount(workspaceDir);
        console.error(`[caroline] [control:ratatosk_own_account_register] ok=${result.ok} email=${result.ok ? result.email : "n/a"} error=${result.ok ? "n/a" : result.error}`);
        send({
          type: "control_response", op, ok: result.ok,
          stdout: result.ok ? JSON.stringify({ email: result.email }) : undefined,
          stderr: result.ok ? undefined : result.error,
        });
        break;
      }
      case "open_login_from_settings": {
        // Same native login form ensure_squirrelwisdom_login opens (also
        // covers "Register", see login.ts's DocumentViewerWindow -- Part 2/5),
        // triggered directly from Settings' button instead of a chat tool
        // call.
        console.error(`[caroline] [control:open_login_from_settings] opening login form`);
        openLoginRequest(send);
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "open_payment_from_settings": {
        // Part 5's "payment" viewer kind -- WebView2 navigates straight at
        // Revolut's own hosted checkout_url, no custom checkout page of ours.
        console.error(`[caroline] [control:open_payment_from_settings] creating topup checkout url`);
        try {
          const checkoutUrl = await createTopupCheckoutUrl();
          console.error(`[caroline] [control:open_payment_from_settings] checkout url created`);
          send({ type: "open_payment", requestId: randomUUID(), checkoutUrl });
        } catch (err) {
          console.error(`[caroline] [control:open_payment_from_settings] failed: ${err instanceof Error ? err.message : String(err)}`);
          send({ type: "control_response", op, ok: false, stderr: err instanceof Error ? err.message : String(err) });
          break;
        }
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "mcp_list": {
        const r = await mcpList(workspaceDir);
        console.error(`[caroline] [control:mcp_list] ok=${r.code === 0}`);
        send({ type: "control_response", op, ok: r.code === 0, stdout: r.stdout, stderr: r.stderr });
        break;
      }
      case "mcp_add": {
        if (!parsed.name || !parsed.command) throw new Error("mcp_add requires name and command");
        // User scope, not the default "local" -- a project-scoped .mcp.json
        // entry sits at "Pending approval" until an interactive `claude`
        // session trusts it, which this headless backend can never do (see
        // workspace.ts for how the default servers hit exactly this trap).
        console.error(`[caroline] [control:mcp_add] name=${parsed.name} command=${parsed.command}`);
        const r = await mcpAdd(workspaceDir, parsed.name, parsed.command, parsed.args ?? [], "user");
        console.error(`[caroline] [control:mcp_add] name=${parsed.name} ok=${r.code === 0}`);
        send({ type: "control_response", op, ok: r.code === 0, stdout: r.stdout, stderr: r.stderr });
        break;
      }
      case "mcp_remove": {
        if (!parsed.name) throw new Error("mcp_remove requires name");
        console.error(`[caroline] [control:mcp_remove] name=${parsed.name}`);
        const r = await mcpRemove(workspaceDir, parsed.name);
        console.error(`[caroline] [control:mcp_remove] name=${parsed.name} ok=${r.code === 0}`);
        send({ type: "control_response", op, ok: r.code === 0, stdout: r.stdout, stderr: r.stderr });
        break;
      }
      case "persona_get": {
        console.error(`[caroline] [control:persona_get] fetching persona state`);
        send({
          type: "control_response", op, ok: true,
          stdout: JSON.stringify({ merged: getPersona(workspaceDir), edit: getPersonaEditState(workspaceDir) }),
        });
        break;
      }
      case "persona_set": {
        const p = parsed.persona;
        if (!p || !p.profileKey) throw new Error("persona_set requires a persona object with profileKey");
        console.error(`[caroline] [control:persona_set] profileKey=${p.profileKey} name=${p.name ?? "n/a"}`);
        setProfileKey(workspaceDir, p.profileKey);
        if (p.profileKey === "custom") {
          saveCustomPersona(workspaceDir, {
            name: p.name ?? "Caroline", gender: p.gender ?? "female", age: p.age ?? "middle-aged", bio: p.bio ?? "",
          });
        } else {
          saveProfileOverride(workspaceDir, p.profileKey, {
            name: p.name, gender: p.gender, age: p.age, bio: p.bio, biography: p.biography, photosDir: p.photosDir,
          });
        }
        console.error(`[caroline] [control:persona_set] saved profileKey=${p.profileKey}`);
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "persona_reset": {
        if (parsed.profileKey !== "caroline" && parsed.profileKey !== "peter") {
          throw new Error("persona_reset requires profileKey 'caroline' or 'peter'");
        }
        console.error(`[caroline] [control:persona_reset] profileKey=${parsed.profileKey}`);
        resetProfile(workspaceDir, parsed.profileKey);
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "visual_mode_get": {
        const model = resolveVisualModel(workspaceDir);
        console.error(`[caroline] [control:visual_mode_get] enabled=${isVisualModeEnabled(workspaceDir)} available=${model !== null} source=${model?.source ?? "null"}`);
        send({
          type: "control_response", op, ok: true,
          stdout: JSON.stringify({
            enabled: isVisualModeEnabled(workspaceDir),
            // "available" -- persona is caroline/peter AND that day's .xcfa actually exists on
            // disk (see resolveVisualModel) -- Settings uses this to grey out the toggle for a
            // custom profile, per explicit instruction ("виден, но disabled").
            available: model !== null,
            source: model?.source ?? null,
          }),
        });
        break;
      }
      case "visual_mode_set": {
        if (typeof parsed.enabled !== "boolean") throw new Error("visual_mode_set requires a boolean 'enabled'");
        console.error(`[caroline] [control:visual_mode_set] enabled=${parsed.enabled}`);
        setVisualModeEnabled(workspaceDir, parsed.enabled);
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "editor_result": {
        if (!parsed.requestId || !parsed.outcome || !parsed.path) {
          throw new Error("editor_result requires requestId, outcome, and path");
        }
        console.error(`[caroline] [control:editor_result] requestId=${parsed.requestId} outcome=${parsed.outcome} path=${parsed.path}`);
        const req = takeViewerRequest(parsed.requestId);
        if (req?.remotePath && parsed.outcome !== "error") {
          // A document opened via the OnlyOffice flow -- pull back whatever
          // got saved server-side (see officeEditor.ts) and clean up the
          // temp copy, before telling Caroline anything. Best-effort: a
          // sync failure here shouldn't also swallow the close notification.
          try {
            await finishOfficeEditSession(req.remotePath, parsed.path);
          } catch (err) {
            console.error(`[caroline] failed to sync office edits back for ${parsed.path}:`, err);
          }
        }
        // A new proactive turn, not a resolved tool call -- open_in_viewer
        // already returned when the window opened (see its doc comment for
        // why blocking on this would be wrong). Visible to the user like a
        // reminder firing, since "the document got saved" is something
        // worth them seeing too, not just Caroline.
        const outcomeText = parsed.outcome === "saved"
          ? `saved (${parsed.path})`
          : parsed.outcome === "cancelled"
            ? `cancelled -- no changes saved (${parsed.path})`
            : parsed.outcome === "closed"
              ? `closed (view-only, ${parsed.path})`
              : `failed to open -- ${parsed.message || "unknown error"} (${parsed.path})`;
        session?.injectProactive(
          `The viewer window you opened for ${parsed.path} just closed: ${outcomeText}. ` +
            `Nobody prompted you for this -- react to it now if it's relevant (e.g. continue whatever the ` +
            `user asked you to do with this file once it was edited).`,
        );
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "login_submit": {
        if (!parsed.requestId) throw new Error("login_submit requires requestId");
        console.error(`[caroline] [control:login_submit] requestId=${parsed.requestId} cancelled=${!!parsed.cancelled} isRegister=${!!parsed.isRegister}`);
        takeLoginRequest(parsed.requestId); // just clears the tracking entry
        if (parsed.cancelled) {
          session?.injectProactive("[The user closed the SquirrelWisdom login form without logging in.]", true);
          send({ type: "control_response", op, ok: true });
          break;
        }
        if (!parsed.email || !parsed.password) throw new Error("login_submit requires email and password unless cancelled");
        const result = parsed.isRegister
          ? await registerAndSaveLogin(parsed.email, parsed.password)
          : await verifyAndSaveLogin(parsed.email, parsed.password);
        console.error(`[caroline] [control:login_submit] isRegister=${!!parsed.isRegister} email=${parsed.email} ok=${result.ok}`);
        if (result.ok) {
          session?.injectProactive(
            `[SquirrelWisdom ${parsed.isRegister ? "registration" : "login"} succeeded for ${parsed.email}. ` +
              `Notes and other SquirrelWisdom-backed tools will work from now on -- no need to log in again.]`,
            true,
          );
        } else {
          // Reopen the same form with the error shown, bypassing the model
          // entirely -- this is a credential retry, not something Caroline
          // needs to decide anything about.
          send({ type: "open_login", requestId: randomUUID(), error: result.error });
        }
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "open_file": {
        if (!parsed.path) throw new Error("open_file requires path");
        console.error(`[caroline] [control:open_file] path=${parsed.path}`);
        if (!existsSync(parsed.path)) {
          console.error(`[caroline] [control:open_file] path=${parsed.path} not found`);
          send({ type: "control_response", op, ok: false, stderr: `No such file: ${parsed.path}` });
          break;
        }
        openFileWithDefaultApp(parsed.path);
        console.error(`[caroline] [control:open_file] path=${parsed.path} opened with default app`);
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "stt": {
        if (!parsed.audioBase64 || !parsed.format) throw new Error("stt requires audioBase64 and format");
        console.error(`[caroline] [stt] requestId=${parsed.requestId} format=${parsed.format} audioBytes=${parsed.audioBase64.length}`);
        const text = await transcribeAudio(parsed.audioBase64, parsed.format, await swSessionOrUndefined());
        console.error(`[caroline] [stt] requestId=${parsed.requestId} ok, text=${truncateForLog(text)}`);
        // requestId echoed back -- these run un-awaited (see the ws
        // "message" handler's `void handleControlRequest(...)`), so
        // multiple concurrent stt/tts calls can genuinely finish out of
        // request order; the client matches replies by id instead of
        // assuming FIFO order (confirmed live: it doesn't hold).
        send({ type: "control_response", op, ok: true, stdout: text, requestId: parsed.requestId });
        break;
      }
      case "tts": {
        if (!parsed.text) throw new Error("tts requires text");
        console.error(`[caroline] [tts] requestId=${parsed.requestId} text=${truncateForLog(parsed.text)}`);
        const swSession = await swSessionOrUndefined();
        // Strips markdown/HTML and normalizes numerals into their spoken, correctly-
        // inflected form before synthesis -- see cleanTextForSpeech's own doc comment.
        // Applies to both automatic voice-reply narration and the manual per-message
        // 🔊 button, and (since Visual Mode's talking-head video is rendered against
        // this SAME synthesized audio) Visual Mode's animation gets the same benefit
        // for free, with no separate change needed there.
        const cleanedText = await cleanTextForSpeech(parsed.text, swSession);
        const audioBase64 = await synthesizeSpeech(
          cleanedText, voiceForGender(getPersona(workspaceDir).gender), swSession);
        console.error(`[caroline] [tts] requestId=${parsed.requestId} ok, audioBytes=${audioBase64.length}`);
        send({ type: "control_response", op, ok: true, stdout: audioBase64, requestId: parsed.requestId });
        break;
      }
      case "shutdown_sync": {
        // Best-effort: the WPF shell calls this before killing the backend
        // process, but Process.Kill() doesn't wait for a real answer here
        // (see BackendProcess.Dispose) -- this just gets the nudge queued
        // as fast as possible, no guarantee it finishes before the process
        // dies mid-tool-call. Deliberately the PRIMARY tab specifically
        // (not the calling `session`, if any) -- the whole app is closing,
        // not just one tab, and the memory backup itself writes one shared
        // "Caroline:Vault" note regardless of which tab does it, so there's
        // no reason for every open tab to race to do the same write.
        console.error(`[caroline] [control:shutdown_sync] nudging primary session for memory backup before shutdown`);
        primarySession()?.injectProactive(
          "The app is closing right now. " + BACKUP_NUDGE + " Do it immediately, as briefly as possible.",
          true,
        );
        send({ type: "control_response", op, ok: true });
        break;
      }
      case "get_history": {
        // See history.ts's doc comment -- rebuilds the visible chat transcript
        // from the real Claude Code session transcript when the chat UI's own
        // localStorage echo is empty (e.g. after an origin change).
        const entries = readRecentHistory(workspaceDir);
        console.error(`[caroline] [control:get_history] entries=${entries.length}`);
        send({ type: "control_response", op, ok: true, stdout: JSON.stringify(entries) });
        break;
      }
      case "expand_dehydrated_ref": {
        // Per explicit instruction (2026-09-09): a recovered chat bubble
        // (get_history) that quotes one of dehydrate.ts's archive/dehydration
        // notes is useless to a human as raw prose with a file path in it --
        // this lets the frontend click-to-expand it in place. `filePath` is
        // whatever chat.js pulled out of the note text client-side via the
        // exact same regex dehydrate.ts exports (extractDehydratedFilePath) --
        // never trust it blindly: resolve and require it to actually be
        // inside workspace/dehydrated/ before touching the filesystem at all,
        // since this is a path handed back by the client.
        if (!parsed.filePath) throw new Error("expand_dehydrated_ref requires filePath");
        const requestId = parsed.requestId;
        const allowedDir = resolve(dehydratedDir(workspaceDir));
        const requestedPath = resolve(String(parsed.filePath));
        if (requestedPath !== allowedDir && !requestedPath.startsWith(allowedDir + sep)) {
          console.error(`[caroline] [expand_dehydrated_ref] rejected path outside dehydrated/: ${requestedPath}`);
          send({ type: "control_response", op, ok: false, stderr: "Path is not inside workspace/dehydrated/", requestId });
          break;
        }
        try {
          if (extname(requestedPath).toLowerCase() === ".txt") {
            const entries = readArchivedEntries(requestedPath);
            console.error(`[caroline] [expand_dehydrated_ref] text archive ${requestedPath}: ${entries.length} entries`);
            send({ type: "control_response", op, ok: true, stdout: JSON.stringify({ kind: "text", entries }), requestId });
          } else {
            const bytes = await readFileAsync(requestedPath);
            const ext = extname(requestedPath).toLowerCase().replace(".", "");
            const mimeByExt: Record<string, string> = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", pdf: "application/pdf" };
            const mimeType = mimeByExt[ext] || "application/octet-stream";
            console.error(`[caroline] [expand_dehydrated_ref] media ${requestedPath}: ${bytes.length} bytes, mime=${mimeType}`);
            send({ type: "control_response", op, ok: true, stdout: JSON.stringify({ kind: "media", mimeType, dataBase64: bytes.toString("base64") }), requestId });
          }
        } catch (err) {
          console.error(`[caroline] [expand_dehydrated_ref] failed to read ${requestedPath}:`, err);
          send({ type: "control_response", op, ok: false, stderr: err instanceof Error ? err.message : String(err), requestId });
        }
        break;
      }
      default:
        console.error(`[caroline] [control:unknown] unrecognized op=${op ?? "unknown"}`);
        send({ type: "control_response", op: op ?? "unknown", ok: false, stderr: `Unknown control op: ${op}` });
    }
  } catch (err) {
    console.error(`[caroline] [control:${op ?? "unknown"}] failed: ${err instanceof Error ? err.stack ?? err.message : String(err)}`);
    send({ type: "control_response", op: op ?? "unknown", ok: false, stderr: String(err) });
  }
}

// One ChatSession per open tab (see MainWindow.xaml.cs's tab strip, capped
// at MAX_TABS there) -- keyed by the tabId each tab's WebView2 puts on its
// WS connection URL (?tab=<id>). Replaces the old single `activeSession`:
// multiple tabs now genuinely run concurrent, independent conversations.
const sessions = new Map<string, ChatSession>();

function primarySession(): ChatSession | undefined {
  return sessions.get(PRIMARY_TAB_ID);
}

// Per tabId, checked once at module load -- a leftover pending-turn-<tab>.json
// means THAT tab's previous process lifetime (not just an in-process
// watchdog restart, which already replays from memory in handleFailure) was
// closed or crashed mid-turn. Read WITHOUT deleting (peekPendingTurn -- see
// its doc comment for why: a delete-on-read here, before any WS client had
// connected to receive the injection, is exactly what silently lost the
// user's message during 2026-08-31's restart storm). The file gets cleaned
// up naturally once the resume attempt below is actually submitted: submit()
// calls savePendingTurn() again (overwriting this with the resume text
// itself), and clearPendingTurn() fires on that turn's own completion just
// like any other turn -- so an interruption of the resume attempt itself
// still leaves a recoverable trace instead of a silent, permanent loss.
// Resolved lazily per tabId (not eagerly for all 5 possible tabs at module
// load) since which tabs actually existed last run isn't known until each
// one's WS connection arrives and says its own tabId.
const resumedUnfinishedTurnForTab = new Set<string>();

// Guards the startup greeting (STARTUP_GREETING_NUDGE) to exactly once per
// backend-process lifetime -- a full app relaunch gets a fresh process (and
// so a fresh greeting), but reconnecting the SAME primary tab's WS (a
// WebView2 reload, say) must not re-greet every time.
let hasGreeted = false;
let hasSentVisualModeConfig = false;

/**
 * Local REST control API -- lets third-party apps on this machine drive
 * Caroline the same way the WPF chat UI does, without speaking the
 * streaming WebSocket protocol: send instructions, read status, and get/set
 * configuration. Loopback-only (same 127.0.0.1 binding as the WS server),
 * same trust model as everything else here -- no separate auth token, since
 * anything already running as this OS user could otherwise just as easily
 * drive the WPF UI directly.
 *
 *   POST /api/message   { text, attachments? }         -> queues a chat turn
 *   GET  /api/status                                    -> session snapshot
 *   POST /api/control   { op, ...same shape as the WS
 *                          "control_request" op body }  -> one control op
 *                          (mcp_list/add/remove, persona_get/set/reset,
 *                          auth_status/logout, open_file, stt, tts -- not
 *                          auth_login, which streams output over the WS
 *                          protocol instead of a single response)
 *
 * Shares the WS server's port (attached to the same http.Server) rather
 * than opening a second one, so there's a single well-known port to point
 * external tools at.
 */
function sendJson(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Content-Length": Buffer.byteLength(text) });
  res.end(text);
}

async function readJsonBody(req: IncomingMessage): Promise<any> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  const raw = Buffer.concat(chunks).toString("utf-8");
  return raw ? JSON.parse(raw) : {};
}

async function handleHttpRequest(req: IncomingMessage, res: ServerResponse): Promise<void> {
  try {
    const url = new URL(req.url ?? "/", "http://localhost");
    if (req.method === "POST" && url.pathname === "/api/message") {
      const body = await readJsonBody(req);
      if (typeof body.text !== "string" || body.text.length === 0) {
        return sendJson(res, 400, { ok: false, error: "text is required" });
      }
      // Tab-agnostic caller -- targets the primary tab by default; pass
      // ?tab=<id> (matching a tab's own id, see MainWindow's tab strip) to
      // reach a different one specifically.
      const tabId = url.searchParams.get("tab") ?? PRIMARY_TAB_ID;
      const target = sessions.get(tabId);
      if (!target) return sendJson(res, 503, { ok: false, error: `No active session for tab "${tabId}" -- open that tab in the Caroline window at least once first.` });
      target.submitOrBranch(body.text, Array.isArray(body.attachments) ? body.attachments : []);
      return sendJson(res, 202, { ok: true });
    }
    if (req.method === "GET" && url.pathname === "/api/status") {
      const primary = primarySession();
      // Primary tab's own detail (what BackendHealthWatchdog polls for --
      // one representative signal is enough for "is the backend frozen",
      // the same reasoning as before multi-tab existed) plus a lightweight
      // summary of every other open tab, for visibility/debugging.
      return sendJson(res, 200, {
        ok: true,
        connected: primary !== undefined,
        ...(primary?.getStatus() ?? {}),
        tabs: Array.from(sessions.entries()).map(([tabId, s]) => ({ tabId, ...s.getStatus() })),
      });
    }
    if (req.method === "POST" && url.pathname === "/api/control") {
      const body = await readJsonBody(req);
      if (body.op === "auth_login") {
        return sendJson(res, 400, { ok: false, error: "auth_login streams output over the WebSocket protocol; not available via this REST endpoint." });
      }
      const tabId = url.searchParams.get("tab") ?? PRIMARY_TAB_ID;
      const result = await new Promise<any>((resolve) => {
        void handleControlRequest(body, (event) => {
          if (event.type === "control_response") resolve(event);
        }, sessions.get(tabId));
      });
      return sendJson(res, 200, result);
    }
    sendJson(res, 404, { ok: false, error: "Not found" });
  } catch (err) {
    sendJson(res, 500, { ok: false, error: err instanceof Error ? err.message : String(err) });
  }
}

const httpServer = createServer((req, res) => {
  // Upgrade requests (the WS chat protocol) are handled by wss below, not here.
  if (req.headers.upgrade) return;
  void handleHttpRequest(req, res);
});
const wss = new WebSocketServer({ server: httpServer });
httpServer.listen(PORT, "127.0.0.1", () => {
  console.log(`[caroline] backend listening on ws://127.0.0.1:${PORT} (and http://127.0.0.1:${PORT}/api/*)`);
});

wss.on("connection", (ws: WebSocket, req: IncomingMessage) => {
  // Each tab's WebView2 opens its own WS connection to ws://127.0.0.1:PORT/?tab=<id>
  // (see MainWindow's per-tab chat.html/chat.js) -- an older, tab-unaware
  // client (or a manual/dev connection) that omits ?tab= lands on the
  // primary tab, same as every single-session client before multi-tab
  // existed.
  const tabId = new URL(req.url ?? "/", "http://localhost").searchParams.get("tab") ?? PRIMARY_TAB_ID;
  console.error(`[caroline] wss connection: tabId=${tabId}`);
  const send = (event: OutEvent) => {
    if (ws.readyState === ws.OPEN) ws.send(JSON.stringify(event));
  };
  const session = new ChatSession(send, tabId);
  sessions.set(tabId, session);
  send({ type: "caroline_status", status: "connected" });

  // Per explicit instruction: resolved once at Caroline's own startup (persona +
  // day-parity), not re-checked before every reply -- see resolveVisualModel's
  // doc comment. The WPF shell uses this to warm a PreparedModel in the
  // background so the first Visual Mode reply of the day isn't stuck behind a
  // ~13-18s render-prep cost. Primary tab only, once per backend-process
  // lifetime -- same "once at startup" scope as the greeting below.
  if (tabId === PRIMARY_TAB_ID && !hasSentVisualModeConfig) {
    hasSentVisualModeConfig = true;
    const model = resolveVisualModel(workspaceDir);
    console.error(`[caroline] wss connection: visual_mode_config enabled=${isVisualModeEnabled(workspaceDir)} modelPath=${model?.modelPath ?? "null"}`);
    send({ type: "visual_mode_config", enabled: isVisualModeEnabled(workspaceDir), modelPath: model?.modelPath ?? null });
  }

  // Per explicit instruction: Caroline must never come back up silently.
  // Fires once per backend-process lifetime (hasGreeted), tied to the
  // primary tab specifically -- same channel reminders/handleFailure treat
  // as "Caroline herself" (see primarySession()). Language is resolved via
  // the real detectLanguage API (see detectRecentLanguage's doc comment),
  // not guessed, and doesn't block accepting the connection.
  if (tabId === PRIMARY_TAB_ID && !hasGreeted) {
    hasGreeted = true;
    void (async () => {
      const lang = await detectRecentLanguage();
      console.error(`[caroline] wss connection: sending startup greeting (lang=${lang})`);
      session.injectProactive(STARTUP_GREETING_NUDGE[lang], false);
    })();
  }

  if (!resumedUnfinishedTurnForTab.has(tabId)) {
    resumedUnfinishedTurnForTab.add(tabId);
    const unfinishedTurn = peekPendingTurn(workspaceDir, tabId);
    if (unfinishedTurn) {
      session.injectProactive(
        `[Caroline was restarted (app closed or crashed) while still working on this, and it was never ` +
          `finished or answered:\n\n"${unfinishedTurn.text}"\n\nResume it now and answer the ` +
          `user -- they don't know this happened yet, so tell them you got interrupted and pick up where ` +
          `you left off. Don't just re-run everything from scratch if you're not sure what already ` +
          `completed -- check first where that makes sense (e.g. was an email already sent, a file ` +
          `already written).]`,
      );
    }
  }

  ws.on("message", (raw) => {
    try {
      const parsed = JSON.parse(raw.toString());
      console.error(`[caroline] [ws] tabId=${tabId} message type=${parsed.type ?? "unknown"}`);
      if (parsed.type === "user_message" && typeof parsed.text === "string") {
        session.submitOrBranch(parsed.text, Array.isArray(parsed.attachments) ? parsed.attachments : [], !!parsed.voice);
      } else if (parsed.type === "interrupt") {
        session.stop();
      } else if (parsed.type === "control_request") {
        void handleControlRequest(parsed, send, session);
      } else {
        console.error(`[caroline] [ws] tabId=${tabId} unhandled message type=${parsed.type ?? "unknown"}`);
      }
    } catch (err) {
      console.error("[caroline] bad message from client:", err);
    }
  });

  ws.on("close", () => {
    session.dispose();
    if (sessions.get(tabId) === session) sessions.delete(tabId);
  });
});

// Checked every 20s (plus once immediately, catching anything that came due
// while the app was closed); a reminder is only marked fired once it's
// actually been injected into a live session, per injectProactive's contract.
// Always the PRIMARY tab (see PRIMARY_TAB_ID) -- reminders are Caroline's
// own proactive behavior (memory backup, checking things), not something
// that should fire once per open tab.
startDueCheckLoop(workspaceDir, (reminder: Reminder) => {
  // Background tasks wait for a quiet moment instead of interrupting a live
  // back-and-forth; priority tasks (the default) fire regardless -- see the
  // Reminder.priority doc comment in scheduler.ts.
  if (reminder.priority === "background" && primarySession()?.hasLiveDialog()) {
    return false;
  }
  const isBackupNudge = reminder.kind === "vault-backup-hourly";
  const delivered = primarySession()?.injectProactive(
    `⏰ Reminder due (you scheduled this for ${reminder.dueAtIso}): ${reminder.note}\n\n` +
      `Nobody prompted you for this -- it's a self-scheduled follow-up. Act on it now and tell ` +
      `the user proactively, don't wait for them to say anything first.`,
    isBackupNudge,
  ) ?? false;
  if (delivered) console.log(`[caroline] reminder ${reminder.id} delivered`);
  return delivered;
});

// Headless Ratatosk owner-DM channel (see ratatoskChannel.ts) -- a tab with
// no WebView2/WS connection at all, created lazily (only once there's an
// actual message to inject, not eagerly at backend startup) so a session
// that never opted into the Ratatosk integration never pays a whole extra
// MCP-heavy query() session for nothing. `send` just logs -- there's no
// chat window to render this conversation in (confirmed with the user: this
// is deliberately headless, not a 6th tab).
const RATATOSK_TAB_ID = "ratatosk";
let ratatoskSession: ChatSession | null = null;

// Per explicit instruction (2026-09-01): the owner-DM channel must never go
// silent. Confirmed live: a turn triggered by an owner message can end
// (SDK "result") WITHOUT ever calling ratatosk_send_message -- a hung tool
// call plus hitting the account's rate limit produced a synthetic "Not
// logged in" reply that never reached Ratatosk at all, leaving the owner
// with no idea anything happened. Tracked per-turn: reset right before
// injecting an owner message, set true the moment a ratatosk_send_message
// tool_use is observed, checked when that turn's "result" arrives -- if
// still false, send a fallback notice ourselves so the human always gets
// SOME answer, even a degraded one.
let ratatoskTurnGotReply = false;

async function notifyOwnerRatatoskTurnHadNoReply(): Promise<void> {
  try {
    if (!hasOwnRatatoskAccount(workspaceDir) || !isLoggedIn()) return;
    const carolineEmail = ownRatatoskEmail(workspaceDir);
    const ownerEmail = loggedInEmail();
    if (!carolineEmail || !ownerEmail) return;
    const session = await getOwnV2Session(workspaceDir);
    const groupId = await findOrCreateDM(session, carolineEmail, ownerEmail);
    await sendMessage(session, groupId, carolineEmail,
      "⚠️ Не смогла нормально ответить на предыдущее сообщение (сбой или лимит) -- напишите ещё раз, если это всё ещё актуально.");
    console.error("[caroline] [ratatosk-session] sent fallback no-reply notice to owner");
  } catch (err) {
    console.error("[caroline] [ratatosk-session] notifyOwnerRatatoskTurnHadNoReply threw:", err);
  }
}

function getOrCreateRatatoskSession(): ChatSession {
  if (ratatoskSession) return ratatoskSession;
  const session = new ChatSession(
    (event) => {
      console.error(`[caroline] [ratatosk-session] ${JSON.stringify(event).slice(0, 500)}`);
      if (event.type !== "sdk_message") return;
      const message = event.message as any;
      if (message?.type === "assistant") {
        for (const block of message.message?.content ?? []) {
          if (block?.type === "tool_use" && block.name === "mcp__caroline-ratatosk__ratatosk_send_message") {
            ratatoskTurnGotReply = true;
          }
        }
      } else if (message?.type === "result") {
        if (!ratatoskTurnGotReply) void notifyOwnerRatatoskTurnHadNoReply();
      }
    },
    RATATOSK_TAB_ID,
  );
  sessions.set(RATATOSK_TAB_ID, session);
  ratatoskSession = session;
  return session;
}
startRatatoskOwnerChannel(workspaceDir, (text) => {
  ratatoskTurnGotReply = false;
  getOrCreateRatatoskSession().injectProactive(text, false);
});
startRatatoskPresenceHeartbeat(workspaceDir);

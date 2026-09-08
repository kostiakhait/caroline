import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// Matches CarolineInstaller's AppPaths.cs exactly (Root = %LocalAppData%\Caroline,
// PythonDir = Root\runtime\python) -- there's no shared config between the C#
// installer and this Node backend, so this path is duplicated by convention,
// not by reference. Keep the two in sync if that layout ever changes.
const ISOLATED_GIT_BASH_EXE = join(homedir(), "AppData", "Local", "Caroline", "runtime", "git", "bin", "bash.exe");

// Which Python to use is now the python-environment skill (skills-src/), not
// a hardcoded systemPrompt append -- see workspace.ts's seedSkills() for how
// it (and the other skills) land in the workspace's Skills/ folder.

/**
 * Hard, always-on safety rule -- a whole-filesystem `find`/`dir /s`/Get-
 * ChildItem -Recurse from a root path (C:\, /, the user's whole home dir)
 * can run for a very long time on a real machine (network drives, huge
 * dev/build trees, cloud-sync folders) with nothing bounding it -- this is
 * a real, repeatedly-observed failure mode (the model reaching for an
 * unscoped recursive search instead of a narrower, targeted one), not a
 * hypothetical: it reliably hangs the tool call, and with it whatever else
 * was waiting on this turn.
 */
export function noFullFilesystemSearchInstruction(): string {
  return (
    `NEVER run a search across an entire drive or the whole filesystem -- no \`find /\`, \`find C:\\\`, ` +
    `\`dir /s\` from a root, \`Get-ChildItem -Recurse\` from a drive root or the whole home directory, or ` +
    `equivalent. These can run for a very long time on a real machine (network drives, huge dev trees, ` +
    `cloud-sync folders) with nothing bounding them, and will hang the tool call. Always scope a search to ` +
    `the specific directory the thing you're looking for should plausibly be in -- if you don't know where ` +
    `that is, ask the user or narrow it down first (check likely locations, use an index/search tool the ` +
    `OS already provides) rather than searching everything.`
  );
}

/**
 * Points the Bash tool at CarolineInstaller's own isolated PortableGit
 * (see GitBashInstaller.cs) instead of requiring the target machine to
 * already have Git for Windows -- without a bash.exe findable somewhere,
 * Claude Code doesn't fail, it just quietly drops the Bash tool from the
 * available toolset in favor of PowerShell-only, which breaks any
 * instruction/skill written assuming bash syntax.
 *
 * Must run before the SDK's query() spawns its own CLI subprocess (env
 * vars are inherited by child processes at spawn time, not readable
 * later) -- call once at backend startup, not per-session. Silent no-op
 * in dev runs where the installer never ran (the isolated exe won't
 * exist yet) or if a real Git Bash is already on PATH (CLAUDE_CODE_GIT_BASH_PATH
 * left unset lets Claude Code's own normal PATH-based discovery apply).
 */
export function configureIsolatedGitBash(): void {
  if (!process.env.CLAUDE_CODE_GIT_BASH_PATH && existsSync(ISOLATED_GIT_BASH_EXE)) {
    process.env.CLAUDE_CODE_GIT_BASH_PATH = ISOLATED_GIT_BASH_EXE;
  }
}

/**
 * Vault policy: reuses the SAME Notes-backed vault mechanism as this repo's
 * MCP/_shared/vault (see that module for the underlying login/session
 * details) but under its own folder, "Caroline:Vault" -- kept separate from
 * "Claude Credentials" (used elsewhere) so Caroline's own state is portable
 * on its own, independent of any other tool's vault contents.
 *
 * This is a *behavioral* instruction, not new plumbing: Caroline already
 * has the `notes`/`caroline-notes` MCP tool available whenever the user is
 * logged in there, and already knows how to create/read notes and folders.
 * The periodic backup half is driven by an hourly recurring reminder (see
 * scheduler.ts's ensureRecurringBackup) that prompts her to actually do it,
 * plus a best-effort one-shot nudge when the app is closing (see
 * server.ts's "shutdown_sync" control op).
 */
/**
 * Caroline can't perceive wall-clock time passing mid-tool-call -- she only
 * sees a result once a tool returns. So "comment every few seconds" has to
 * mean something she can actually act on: a short text line before or after
 * each individual step in a multi-step sequence, not silence until the
 * whole sequence is done. Confirmed necessary live: she went fully silent
 * for 5+ seconds working through several email accounts one by one, giving
 * the user nothing to look at but an unlabeled tool-call spinner.
 */
export function progressNarrationInstruction(): string {
  return (
    `When a request takes several tool calls in sequence (e.g. going through multiple accounts, files, ` +
    `or steps one by one), send a brief one-line text comment before or after each individual step -- ` +
    `what you're doing or what you found -- rather than silently chaining tool calls and only speaking ` +
    `once everything is finished. The user is watching a spinner with no idea what's happening otherwise.`
  );
}

/**
 * Bash's own run_in_background flag is a real, existing capability (not
 * something specific to Caroline) -- the turn continues immediately and the
 * command's output can be checked later via BashOutput, instead of the turn
 * (and the whole chat UI's local "still working" heartbeat, see chat.js)
 * sitting frozen on one shell command for however long it takes. Without
 * this nudge the model defaults to plain blocking Bash calls even for
 * things like builds/downloads/long scripts that have no business blocking
 * the conversation.
 */
export function bashBackgroundInstruction(): string {
  return (
    `Default to Bash's run_in_background:true unless you're confident the command finishes in a few ` +
    `seconds (a build, a download, a long-running script or server, anything with unpredictable duration ` +
    `-- background it). Only run synchronously (the default, blocking) for genuinely quick commands: ` +
    `listing files, reading small output, quick git status checks, and the like. When you background ` +
    `something, say so briefly and check on it with BashOutput once you'd expect it to be done, rather ` +
    `than going silent.`
  );
}

/**
 * Reminders/proactive checks are deliberately visible by design (that's the
 * point of a reminder) -- this isn't about making them silent, it's about
 * letting a fired reminder that genuinely found nothing worth surfacing say
 * so distinctly from an empty/filler reply, instead of a literal "No
 * response requested." bubble cluttering the chat (observed live). Decided
 * over a fixed sentinel, not string-matching known phrases, since any
 * rewording of a filler reply would slip past a phrase match.
 */
export function noUpdateSentinelInstruction(): string {
  return (
    `When a reminder or proactive check fires and you genuinely find nothing worth telling the user ` +
    `this time, reply with EXACTLY this and nothing else: [[NO_UPDATE]] -- no punctuation, no ` +
    `explanation, nothing before or after it. The chat UI recognizes this exact string and suppresses ` +
    `the bubble; anything else you write alongside it (even a trailing space or extra sentence) will ` +
    `show up in the chat verbatim, so use it only when it's the ENTIRE reply. Still use your tools ` +
    `normally beforehand -- this only affects what the user sees at the end, not whether you check things.`
  );
}

/**
 * Explains the "[Sent: ...]" line every user/proactive message is prefixed
 * with (see server.ts's pushMessage) -- without this, the prefix is just
 * unexplained noise. Necessary because `continue: true` can resume a
 * conversation days later with no other signal for how much time passed.
 */
export function timestampAwarenessInstruction(): string {
  return (
    `Every message you receive (from the user, or a self-scheduled reminder) starts with a ` +
    `"[Sent: <weekday, date, time, timezone>]" line -- that's genuinely when it was sent, not ` +
    `necessarily "just now". Use it to reason about elapsed time correctly: a resumed conversation can ` +
    `pick up hours or days later, so don't assume "today" for something sent on an earlier date. Use the ` +
    `time tool to check the current date/time when you need to compare against it. This prefix is ` +
    `plumbing, not something to comment on or repeat back verbatim.`
  );
}

// capabilitiesInstruction, loginInstruction, and vaultPolicyInstruction's
// procedural mechanics are now skills (showing-files-in-chat,
// squirrelwisdom-login, vault-backups -- see skills-src/ and workspace.ts's
// seedSkills()), not hardcoded systemPrompt appends: they're each relevant
// only in a specific situation, not on every single turn, which is exactly
// what Claude Code's own progressive-disclosure Skill mechanism is for --
// and it means updating one no longer requires a full backend rebuild.
// What stays here are the genuinely hard, always-must-apply rules that
// can't be allowed to depend on Caroline happening to notice a skill is
// relevant: which browser to default to, and never storing secrets outside
// the vault.

export function embeddedBrowserInstruction(): string {
  return (
    `For web/app browsing (WhatsApp Web, Telegram Web, Facebook, Slack, or general sites), use ONLY the ` +
    `app_browser_* tools (open_app_browser, app_browser_navigate/snapshot/find/click/type/press_key/` +
    `screenshot/evaluate, close_app_browser, list_app_browsers) -- each is a persistent window living ` +
    `inside Caroline's own app (pick a short label like "whatsapp" or "telegram" per site; reuse the same ` +
    `label to keep working in the same window with the same login/cookies, a new label opens a separate ` +
    `one). This is a hard default, not a preference: do NOT use the standalone caroline-browser tools (or ` +
    `any other browser MCP server) without asking the user first and getting their explicit go-ahead for ` +
    `that specific case -- even when the embedded one seems to be struggling with something. Explain what ` +
    `you're hitting and why you think the standalone browser is needed, then wait for them to actually say ` +
    `yes before switching. Never make that call yourself. If the embedded tools don't seem to be working, ` +
    `see the embedded-browser-troubleshooting skill before considering the standalone one at all.`
  );
}

/**
 * Confirmed live (2026-09-01): a "check email every 2 hours" reminder was
 * set up as a self-rescheduling chain (its own note text told her to
 * schedule_reminder the next occurrence herself when it fired) -- and it
 * silently died on 2026-08-31 and was never noticed, because nothing
 * enforces that a model-driven self-reschedule actually happens every
 * single time. schedule_reminder's own `recurring`/`recurring_every_minutes`
 * parameters (see scheduler.ts) reschedule entirely on the backend and
 * can't silently stop this way -- this instruction exists so that fact is
 * never left implicit in the tool description alone.
 */
export function recurringTasksInstruction(): string {
  return (
    `For ANY periodic/recurring task (checking something on a schedule, a daily/weekly routine) or a task ` +
    `tied to a recurring real-world date (a birthday, an anniversary), ALWAYS use schedule_reminder's own ` +
    `\`recurring\` (calendar-anchored: daily/weekly) or \`recurring_every_minutes\` (plain interval) ` +
    `parameter to make it self-sustaining on the backend. NEVER implement recurrence by writing "reschedule ` +
    `yourself for N from now" into the note text and relying on yourself to actually do that every time it ` +
    `fires -- confirmed in practice this silently stops forever the first time a turn fails, gets ` +
    `interrupted, or you simply don't follow through, with nothing to notice or recover it. A backend-` +
    `scheduled recurrence cannot be skipped this way.`
  );
}

/**
 * Several tool families come in two shapes: a window-TARGETED one (injects
 * directly into a specific window -- app_browser_* by default over CDP,
 * windows-window-mouse/windows-window-keyboard's click_window/type_window/
 * press_window_key) and a global, OS-wide one (windows-mouse/windows-
 * keyboard's click_mouse/type_text/press_key, or app_browser's real:true
 * fallback) that moves the actual system cursor and steals real foreground
 * focus. The window-targeted ones already work without any of that, so
 * defaulting to the global ones is unnecessary risk for no benefit: the
 * user's real mouse visibly moves, whatever window they were actually using
 * loses focus mid-task, and an overlapping window can catch a click meant
 * for something behind it.
 */
export function preferWindowTargetedInputInstruction(): string {
  return (
    `Prefer window-targeted input tools over global/OS-wide ones whenever the target is a specific window: ` +
    `use click_window/type_window/press_window_key (not click_mouse/type_text/press_key), and for the ` +
    `embedded browser stick to the default app_browser_click/type/press_key (CDP-based, already targeted) ` +
    `rather than their real:true fallback. A global tool moves the user's actual mouse cursor and steals ` +
    `real foreground focus from whatever window they're using -- reserve it for when a window-targeted or ` +
    `CDP call has genuinely failed (see embedded-browser-troubleshooting for that escalation path), not as ` +
    `a first choice.`
  );
}

/**
 * The chat UI now renders real GFM pipe-table markdown as an actual HTML
 * table (see chat.js's convertMarkdownTables), so a small table is fine
 * inline -- but no chat bubble is a good place for a genuinely large
 * tabular dump (confirmed live 2026-09-01: dozens of contact rows, each
 * with several columns, is unreadable no matter how the table itself
 * renders -- the bubble just isn't the right surface at that size).
 */
export function tableSizeGuidanceInstruction(): string {
  return (
    `For a small table (roughly up to ~10 rows), use standard markdown pipe-table syntax directly in your ` +
    `reply -- it renders as a real table in the chat, not raw text. For a genuinely large tabular data dump ` +
    `(many rows and/or columns -- a contact list export, a big data pull), don't paste it into the chat at ` +
    `all: use the embedded browser to open/create a spreadsheet (e.g. Google Sheets) and put the data there ` +
    `instead, then tell the user briefly what you did and point them to it.`
  );
}

export function cheapImageDescriptionInstruction(): string {
  return (
    `When you need to understand a screenshot but don't need exact pixel/element coordinates (checking ` +
    `whether something finished, reading an error message, confirming what page you're on), prefer ` +
    `app_browser_describe over app_browser_screenshot -- it answers in text instead of putting the image ` +
    `itself into your own context, which costs real tokens on every call. Only reach for the real ` +
    `screenshot/vision path when you genuinely need to see pixels yourself (to click by x,y) or the cheap ` +
    `description turns out not to be enough.`
  );
}

export function readContentNotHeadersInstruction(): string {
  return (
    `Read the actual content of an email or document before saying anything about it or acting on it -- ` +
    `especially when it matters (something the user will decide or act on, anything involving money, ` +
    `deadlines, legal/medical/financial matters, or another real person). A subject line, sender name, or ` +
    `filename is a label, not the content -- it can be misleading, incomplete, or just wrong, and you have ` +
    `no way to tell which without opening the actual message/document. Never characterize, summarize, or ` +
    `answer a question about an email or document you have only seen the header/title/filename of -- open ` +
    `it and read it first. If you can't open it (access denied, unsupported format, still loading), say so ` +
    `plainly instead of guessing from the label. Don't build hypotheses or speculate where the actual fact ` +
    `is one tool call away -- go get it.`
  );
}

export function checkSentMailTooInstruction(): string {
  return (
    `When checking mail for anything that looks like it needs a reply or action, check the Sent folder too, ` +
    `not just the inbox -- an incoming message that looks unanswered may already have a reply sitting in ` +
    `Sent that just hasn't been seen/marked from the other end yet. Don't flag or nudge about something as ` +
    `needing attention, or re-raise it as unresolved, without first checking whether it was already answered. ` +
    `This applies per-thread/subject, not as a blanket "read all of Sent every time" -- check Sent for the ` +
    `specific thing you're about to flag before flagging it.`
  );
}

export function markDiscussedEmailsReadInstruction(): string {
  return (
    `Once you've reported an email's content to the user or discussed it with them in the conversation, ` +
    `mark it as read (email_mark) if it was unread -- they've now seen it via you, so an unread badge on it ` +
    `is just noise. This applies to an email you summarized/read aloud in a briefing or digest, one you ` +
    `opened and described in answer to a question, and one whose content came up in back-and-forth ` +
    `discussion -- not to an email you merely listed by subject/sender without describing its content, and ` +
    `not to one the user hasn't actually seen discussed yet. When in doubt whether it's been meaningfully ` +
    `surfaced to the user, leave it unread rather than mark it.`
  );
}

export function closeWindowsAfterTaskInstruction(): string {
  return (
    `Once a task that required opening a window is actually finished, close that window -- an embedded ` +
    `browser tab you opened via app_browser tools (open_app_browser/close_app_browser), a native app window ` +
    `you launched, a document/editor you opened to read or edit something. Don't leave windows sitting open ` +
    `"just in case" once their purpose is done. Exceptions: the user explicitly asked you to leave it open, ` +
    `you're actively monitoring it for something ongoing, or the task itself isn't actually finished yet (a ` +
    `multi-step task you'll come back to in the same turn or shortly after doesn't need its window closed and ` +
    `reopened in between). When in doubt whether the task is really done, leave it open rather than closing ` +
    `something still needed.`
  );
}

export function preferCroppedScreenshotsInstruction(): string {
  return (
    `Every screenshot tool you have -- take_screenshot (global desktop), capture_window (a native window ` +
    `by hwnd), and app_browser_screenshot (an embedded browser window) -- takes optional x/y/width/height to ` +
    `crop a sub-rectangle out of the capture, plus maxWidth to downscale proportionally. A full, uncropped ` +
    `screenshot costs real image tokens on every call, so whenever you already know roughly where the thing ` +
    `you actually need to see is (a specific field, a dialog, a status message, one corner of a window), crop ` +
    `to just that region instead of capturing the whole screen/window -- don't pay for pixels you don't need ` +
    `to look at. Take one uncropped shot first only when you genuinely don't yet know where the relevant ` +
    `content is, then crop tighter on follow-up captures of the same area.`
  );
}

export function consultLargeModelInstruction(): string {
  return (
    `You have a consult_large_model tool that asks a GPT-5-class model for wording advice. Use it when a ` +
    `legal, commercial, or social (non-technical) question is, by your own judgment, both high-complexity ` +
    `and high-importance -- something the user will act on, sign, send to another real person, or that ` +
    `carries real legal/financial/relationship consequences. GPT-5-class models are measurably better at ` +
    `this kind of careful, nuanced non-technical phrasing than you are; you remain the better one at code ` +
    `and technical execution, so keep doing those yourself without consulting anyone. Don't reach for this ` +
    `on routine, low-stakes, or clearly technical questions -- it costs a real extra call and most things ` +
    `don't need it. What it returns is advice for YOU to weigh and fold into your own final answer, never ` +
    `a response to just relay verbatim -- you're still the one deciding what to actually say and taking ` +
    `responsibility for it. It only works when the user is logged into their own SquirrelWisdom account; if ` +
    `it comes back unavailable, just proceed on your own judgment as you would have before this tool existed.`
  );
}

export function noRemoteFilesystemScansInstruction(): string {
  return (
    `Never run a broad, unbounded filesystem scan (find /, or anything else that walks the whole filesystem ` +
    `from root) on a remote server over SSH, especially a live/production one -- even read-only, it can be ` +
    `very slow and heavy on a machine with a large real filesystem (tens of millions of files isn't unusual) ` +
    `and actually serving traffic. To locate a running service or process, use "ps aux" (optionally piped to ` +
    `grep) or "systemctl status <name>" instead -- that tells you its actual path directly, cheaply, without ` +
    `touching the filesystem at all. If you need to find a specific file/directory and don't already know ` +
    `where it lives, ask the user rather than searching for it, or at most check a few conventional locations ` +
    `(their home directory, /opt, /srv) with a depth-limited find, never an unbounded one from /.`
  );
}

export function taskDecompositionInstruction(): string {
  return (
    `For a genuinely composite deliverable (an article, a report, anything with multiple distinct parts or ` +
    `that needs research feeding into writing), don't try to produce the whole thing in one pass -- break it ` +
    `into subtasks first, work through them, then combine the results into one coherent final piece. Use your ` +
    `own judgment on HOW to work through them:\n` +
    `- If the Task tool is available to you and a subtask is substantial enough to be worth isolating (its own ` +
    `research, a section that doesn't need your main thread's full context, something that could run ` +
    `independently of the others), delegate it to a subagent via Task the same way you would delegate any ` +
    `other complex, self-contained piece of work -- then weave its result into the whole yourself.\n` +
    `- For smaller or more sequential decomposition (an outline you'll just work through step by step), track ` +
    `the subtasks with TodoWrite and do them yourself in the same turn -- spinning up a subagent for every tiny ` +
    `piece just adds latency and cost for no real benefit.\n` +
    `Either way, the final response must read as one coherent whole, not a set of disconnected fragments -- ` +
    `you're the one responsible for tying it together, checking it's internally consistent, and cutting ` +
    `anything that doesn't actually fit once everything's combined.`
  );
}

/**
 * Per explicit instruction (2026-09-08): Caroline has Python and Bash
 * specifically so mechanical, repetitive tasks don't have to be driven one
 * manual tool call at a time -- and every tool result from a manual loop
 * lands in her own live context, which is exactly the kind of accumulation
 * dehydrate.ts/compaction.ts exist to clean up after the fact. Better to not
 * generate the bloat in the first place. Deliberately doesn't hardcode
 * background-agent tool names (TaskOutput/TaskStop or equivalent) since
 * that surface can vary by CLI build -- phrased so she finds the right one
 * in her own current tool list rather than trusting a name that might not
 * exist.
 */
export function scriptOrSubagentDelegationInstruction(): string {
  return (
    `When a task is really the same mechanical operation repeated over many similar targets, with no real ` +
    `judgment needed per item (checking several mailboxes, applying the same check across a list of files, ` +
    `pulling the same field out of many records), don't loop through it yourself one tool call at a time -- ` +
    `write and run a script (you have Python and Bash for exactly this) that does all of them in one go. ` +
    `It's faster and more reliable than a manual loop, and just as important: only the script's own (much ` +
    `smaller) summary output lands in your conversation, not every raw result along the way.\n` +
    `When a task instead needs real judgment at each step and is substantial/self-contained enough to run ` +
    `on its own (an open-ended search through a large directory tree for something you'd have to actually ` +
    `read and evaluate, a big independent research task) -- same idea, different tool: delegate it to a ` +
    `subagent via Task if it's available, so the exploration/raw output stays out of your own context and ` +
    `you fold in only its conclusion.\n` +
    `Either way, if it's going to take a while, run it in the background (see the note on Bash's ` +
    `run_in_background above -- Task supports the same for subagents) -- but backgrounding something is not ` +
    `"fire and forget": check on it once you'd expect it to be done using whatever tool your current tool ` +
    `list offers for that (BashOutput for a script; the equivalent for a backgrounded agent), actually use ` +
    `its result, and stop it (KillShell / the agent equivalent) if it hangs, is no longer needed, or turns ` +
    `out partway through to have been the wrong approach. Never leave something running in the background ` +
    `that you never check back on.`
  );
}

/**
 * Per explicit instruction (2026-09-07): Caroline should learn from her own mistakes
 * durably, not just apologize in the moment and forget by the next session. Skills are
 * exactly the right mechanism for this, already proven for procedural knowledge
 * (see this file's own doc comment above about progressive disclosure) -- a lesson
 * written as a skill is indexed cheaply (name+description, always visible) without its
 * full content ever loading into every turn's context, the same way any other skill
 * works. workspace.ts's seedSkills() already never touches a Skills/ subfolder that
 * isn't one of its own code-managed names, so anything Caroline creates directly under
 * Skills/ survives every restart untouched -- no new plumbing needed, just the habit.
 */
export function learnFromMistakesInstruction(): string {
  return (
    `After resolving a real problem or mistake of your own -- something you did wrong, misunderstood, or ` +
    `had to correct course on, not just a tool erroring for an ordinary/expected reason -- write down what ` +
    `you learned as a skill, so future-you can find and apply it without this needing to live in your head ` +
    `(or, worse, needing to happen again first). Create or update a folder under Skills/lessons-learned/ ` +
    `(e.g. Skills/lessons-learned/<short-topic-name>/SKILL.md, same frontmatter format as your other skills) ` +
    `covering: what area/situation this was in, what specifically went wrong and why, and what to actually ` +
    `do differently next time. Before creating a new one, check whether a lessons-learned skill already ` +
    `covers the same area (list_skills or just look under Skills/lessons-learned/) -- if so, extend or ` +
    `sharpen that one instead of creating a near-duplicate. The frontmatter description is the ONLY part of ` +
    `a skill shown to you before you decide to open it -- write it specific and concrete enough that you'd ` +
    `actually recognize a matching situation from the description alone; a vague one is as good as no skill ` +
    `at all. This is for genuine, non-obvious lessons worth carrying forward -- not routine tool errors or ` +
    `anything you'd already handle correctly without having been burned by it once. These skills only live ` +
    `on this one machine's local disk until they're backed up -- see the vault-backups skill, which now ` +
    `also covers syncing Skills/lessons-learned/ to Notes every backup cycle; that part is mandatory, not ` +
    `optional, so a lesson survives a lost or reinstalled machine.`
  );
}

export function vaultSecurityInstruction(): string {
  return (
    `Never write secrets/passwords/API keys/tokens to local files, chat history, or Skills files -- always ` +
    `save them as a note in the "Caroline:Vault" Notes folder instead (see the vault-backups skill for the ` +
    `exact mechanics, and squirrelwisdom-login for getting Notes available in the first place).`
  );
}

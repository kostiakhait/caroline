import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { z } from "zod";
import { tool, createSdkMcpServer, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";

export interface Reminder {
  id: string;
  dueAtIso: string;
  note: string;
  createdAtIso: string;
  fired: boolean;
  /**
   * "priority" (default, and what every reminder was before this field
   * existed) fires the instant it's due, live dialog or not. "background"
   * only fires when there's no live dialog (see ChatSession.hasLiveDialog)
   * -- if one's in progress when this comes due, it's left un-fired and
   * retried on the next poll, so it naturally waits for a quiet moment
   * instead of interrupting.
   */
  priority?: "background" | "priority";
  /** When set, firing this reminder immediately schedules the next occurrence dueAtIso + recurringMs from now. */
  recurringMs?: number;
  /**
   * Calendar-based recurrence for "every day at 8am" / "every Saturday at
   * 17:30" style schedules -- the next occurrence is computed by adding
   * calendar days (1 or 7) via Date's local-time setters, not a fixed
   * millisecond interval, so it lands on the same wall-clock time even
   * across a DST transition. Takes precedence over recurringMs if both are set.
   */
  recurringCalendar?: "daily" | "weekly";
  /** Tag used to detect "this recurring reminder was already seeded" idempotently across restarts -- see ensureRecurringBackup. */
  kind?: string;
}

function schedulePath(workspaceDir: string): string {
  return join(workspaceDir, "schedule.json");
}

function loadReminders(workspaceDir: string): Reminder[] {
  const path = schedulePath(workspaceDir);
  if (!existsSync(path)) return [];
  try {
    return JSON.parse(readFileSync(path, "utf-8"));
  } catch (err) {
    console.error("[caroline] [scheduler] loadReminders failed, treating as empty:", err);
    return [];
  }
}

function saveReminders(workspaceDir: string, reminders: Reminder[]): void {
  writeFileSync(schedulePath(workspaceDir), JSON.stringify(reminders, null, 2) + "\n", "utf-8");
}

/**
 * An in-process ("SDK-hosted") MCP server -- no child process, runs
 * directly in this backend -- giving Claude tools to schedule/list/cancel
 * her own reminders. Storage is workspace/schedule.json, so it survives
 * both a session restart (the watchdog's) and a full app restart: whatever
 * hasn't fired yet is still there next time the due-check loop starts (see
 * startDueCheckLoop below), which is also what makes an overdue reminder
 * (app was closed when it came due) fire as soon as the app reopens
 * instead of being silently lost.
 */
export function createSchedulerTool(workspaceDir: string): McpServerConfig {
  const scheduleReminder = tool(
    "schedule_reminder",
    "Schedule a reminder/task for yourself (Caroline) to act on at a specific future date/time, " +
      "surviving app restarts. When it comes due you will be prompted with the note text " +
      "automatically, without the user saying anything -- use this for anything the user asks " +
      "you to do 'at' or 'in' some time, or any follow-up you decide you should do later, " +
      "including recurring periodic tasks (e.g. 'every morning check my email').\n\n" +
      "priority controls what happens if this comes due while you're in the middle of something " +
      "with the user: 'priority' (default) fires immediately regardless -- use this when the " +
      "user needs it to happen at that exact time no matter what (e.g. 'at 17:30 sharp, send this " +
      "reminder'). 'background' only fires once there's no live back-and-forth going on -- it's " +
      "deferred and retried later if the user is actively chatting when it comes due. Use " +
      "'background' for routine periodic chores that aren't time-critical (e.g. 'every morning " +
      "check my email and tell me what's important').",
    {
      due_at_iso: z.string().describe("ISO 8601 date-time this should first fire at, including timezone offset, e.g. 2026-08-29T15:00:00-04:00. Use the time tool to know the current date/time first."),
      note: z.string().describe("What to do, or what to tell the user, when this fires"),
      priority: z.enum(["background", "priority"]).optional().describe("Default 'priority'. See tool description for the distinction."),
      recurring: z.enum(["none", "daily", "weekly"]).optional().describe(
        "Default 'none' (fires once). 'daily'/'weekly' reschedule for the same time of day the " +
          "next day/week, every time this fires, indefinitely -- for 'every morning' or 'every " +
          "Saturday at <time>' style requests. Mutually exclusive with recurring_every_minutes -- " +
          "use that instead for a plain fixed-interval cadence (e.g. 'every 2 hours').",
      ),
      recurring_every_minutes: z.number().positive().optional().describe(
        "For a plain fixed-interval recurring task (e.g. 'every 2 hours check my email' -> 120) -- " +
          "reschedules itself automatically, indefinitely, entirely on the backend. ALWAYS use this " +
          "(or 'recurring' above for calendar-anchored cases) for ANY periodic task, instead of " +
          "putting 'reschedule yourself for N from now' instructions in the note text: a note-text " +
          "self-reschedule is fragile -- it silently stops forever the very first time a turn fails, " +
          "gets interrupted, or you simply forget the instruction, with no automatic recovery. This " +
          "parameter never depends on you remembering anything at fire time.",
      ),
    },
    async ({ due_at_iso, note, priority, recurring, recurring_every_minutes }) => {
      console.error(`[caroline] [tool:schedule_reminder] due_at_iso=${due_at_iso} priority=${priority ?? "priority"} recurring=${recurring ?? "none"} recurring_every_minutes=${recurring_every_minutes ?? "n/a"}`);
      const reminders = loadReminders(workspaceDir);
      const reminder: Reminder = {
        id: randomUUID(),
        dueAtIso: due_at_iso,
        note,
        createdAtIso: new Date().toISOString(),
        fired: false,
        priority: priority ?? "priority",
        recurringCalendar: recurring && recurring !== "none" ? recurring : undefined,
        recurringMs: recurring_every_minutes ? recurring_every_minutes * 60_000 : undefined,
      };
      reminders.push(reminder);
      saveReminders(workspaceDir, reminders);
      console.error(`[caroline] [tool:schedule_reminder] scheduled id=${reminder.id}`);
      const recurText = reminder.recurringCalendar
        ? `, recurring ${reminder.recurringCalendar}`
        : reminder.recurringMs ? `, recurring every ${recurring_every_minutes}min` : "";
      return { content: [{ type: "text", text: `Scheduled (id ${reminder.id}) for ${due_at_iso} [${reminder.priority}${recurText}]: ${note}` }] };
    },
  );

  const listReminders = tool(
    "list_reminders",
    "List reminders that have been scheduled but haven't fired yet.",
    {},
    async () => {
      console.error(`[caroline] [tool:list_reminders] invoked`);
      const pending = loadReminders(workspaceDir).filter((r) => !r.fired);
      console.error(`[caroline] [tool:list_reminders] ${pending.length} pending`);
      return { content: [{ type: "text", text: pending.length ? JSON.stringify(pending, null, 2) : "No pending reminders." }] };
    },
  );

  const cancelReminder = tool(
    "cancel_reminder",
    "Cancel a previously scheduled reminder by its id (see list_reminders).",
    { id: z.string() },
    async ({ id }) => {
      console.error(`[caroline] [tool:cancel_reminder] id=${id}`);
      const before = loadReminders(workspaceDir);
      const after = before.filter((r) => r.id !== id);
      saveReminders(workspaceDir, after);
      const removed = after.length < before.length;
      console.error(`[caroline] [tool:cancel_reminder] id=${id} removed=${removed}`);
      return { content: [{ type: "text", text: removed ? `Cancelled ${id}.` : `No pending reminder with id ${id}.` }] };
    },
  );

  return createSdkMcpServer({
    name: "caroline-scheduler",
    tools: [scheduleReminder, listReminders, cancelReminder],
  });
}

/**
 * Computes when a recurring reminder should fire next. recurringCalendar
 * advances by calendar days (1 or 7) via Date's local-time setDate/getDate
 * -- these normalize correctly across a DST transition on the host's local
 * timezone, so "every day/Saturday at 17:30" keeps landing on 17:30 local
 * time instead of drifting an hour twice a year the way a fixed
 * 24h/7-day millisecond interval would. Falls back to the plain
 * millisecond interval (recurringMs) when no calendar recurrence is set.
 */
function nextOccurrence(r: Reminder): Date {
  const now = Date.now();
  if (r.recurringCalendar) {
    const step = r.recurringCalendar === "weekly" ? 7 : 1;
    const base = new Date(r.dueAtIso);
    // If the app was closed long enough to miss one or more occurrences,
    // skip straight to the next one still in the future instead of firing
    // a burst of catch-up reminders for every missed day/week.
    do {
      base.setDate(base.getDate() + step);
    } while (base.getTime() <= now);
    return base;
  }
  return new Date(now + (r.recurringMs ?? 0));
}

/**
 * Polls workspace/schedule.json and calls onDue for every reminder whose
 * time has passed. onDue returns whether it actually got delivered (e.g.
 * there's a live chat session to inject it into) -- a reminder is only
 * marked fired when that's true, so one that comes due while the app
 * happens to be between sessions (or fully closed) stays pending and
 * fires on the very next check instead of being silently dropped.
 */
export function startDueCheckLoop(
  workspaceDir: string,
  onDue: (reminder: Reminder) => boolean,
  intervalMs = 20_000,
): NodeJS.Timeout {
  const check = () => {
    const reminders = loadReminders(workspaceDir);
    const now = Date.now();
    let changed = false;
    for (const r of reminders) {
      if (r.fired) continue;
      if (new Date(r.dueAtIso).getTime() > now) continue;
      if (onDue(r)) {
        r.fired = true;
        changed = true;
        if (r.recurringCalendar || r.recurringMs) {
          reminders.push({
            id: randomUUID(),
            dueAtIso: nextOccurrence(r).toISOString(),
            note: r.note,
            createdAtIso: new Date(now).toISOString(),
            fired: false,
            priority: r.priority,
            recurringMs: r.recurringMs,
            recurringCalendar: r.recurringCalendar,
            kind: r.kind,
          });
        }
      }
    }
    if (changed) saveReminders(workspaceDir, reminders);
  };
  check(); // catch up on anything already overdue right away, don't wait a full interval
  return setInterval(check, intervalMs);
}

/**
 * Seeds a houly recurring "back up your memory" reminder exactly once --
 * detected via the `kind` tag, so restarting the app (or the watchdog
 * restarting a session) never piles up duplicate recurring chains. Safe to
 * call on every startup.
 */
export function ensureRecurringBackup(workspaceDir: string, note: string, intervalMs = 60 * 60_000): void {
  const kind = "vault-backup-hourly";
  const reminders = loadReminders(workspaceDir);
  if (reminders.some((r) => r.kind === kind)) {
    console.error(`[caroline] [scheduler] ensureRecurringBackup: already seeded, skipping`);
    return;
  }
  console.error(`[caroline] [scheduler] ensureRecurringBackup: seeding hourly backup reminder (intervalMs=${intervalMs})`);
  reminders.push({
    id: randomUUID(),
    dueAtIso: new Date(Date.now() + intervalMs).toISOString(),
    note,
    createdAtIso: new Date().toISOString(),
    fired: false,
    recurringMs: intervalMs,
    kind,
  });
  saveReminders(workspaceDir, reminders);
}

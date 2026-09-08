import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { hasOwnRatatoskAccount, ownRatatoskEmail, getOwnV2Session } from "./ratatoskOwnAccount.js";
import { listConversations, getRecentMessages, sendPresenceHeartbeat, getServerNow, type RatatoskConversation } from "./ratatosk.js";

// No push from Ratatosk -- its own UI polls every 4-5s; this owner-DM
// control channel is far less latency-sensitive (it's "give Caroline an
// instruction", not a live conversation someone's staring at), so a longer
// interval is fine and keeps this from hammering the backend.
const POLL_INTERVAL_MS = 15_000;

// Deliberately its OWN, much faster interval -- NOT piggybacked on
// POLL_INTERVAL_MS above. portal/chat.js's presence TTL is 15s; reusing the
// 15s owner-DM poll for the heartbeat too would put every heartbeat right at
// the TTL boundary, so any tick that ran even slightly late (network hiccup,
// event-loop delay) would let Caroline visibly flicker offline. 5s matches
// chat.js's own PRESENCE_POLL_MS exactly, giving 3 heartbeats per TTL window.
const PRESENCE_INTERVAL_MS = 5_000;

// Keyed by groupId -- one cursor per monitored group, not a single global
// value, since (per explicit instruction, 2026-09-03) this channel now
// watches every group Caroline is a member of, not just one hardcoded
// owner DM. Replaces the old ratatosk-owner-dm-cursor.json (single-group)
// format entirely -- a fresh file, not a migration, since that old cursor's
// one timestamp was itself tied to whichever single group used to be
// hardcoded and has no meaningful mapping onto "per group" here.
function cursorsPath(workspaceDir: string): string {
  return join(workspaceDir, "ratatosk-groups-cursor.json");
}

function loadCursors(workspaceDir: string): Record<string, number> {
  try {
    if (!existsSync(cursorsPath(workspaceDir))) return {};
    return JSON.parse(readFileSync(cursorsPath(workspaceDir), "utf-8")) as Record<string, number>;
  } catch (err) {
    console.error("[caroline] [ratatosk-channel] loadCursors failed (treating as never-checked):", err);
    return {};
  }
}

function saveCursors(workspaceDir: string, cursors: Record<string, number>): void {
  try {
    writeFileSync(cursorsPath(workspaceDir), JSON.stringify(cursors, null, 2) + "\n", "utf-8");
  } catch (err) {
    console.error("[caroline] [ratatosk-channel] saveCursors failed (best-effort, next tick re-derives it):", err);
  }
}

/**
 * In-memory snapshot of the channel's own state, for the ratatosk_status_get
 * control op (Settings/external tooling) -- confirmed live (2026-08-31) on a
 * DIFFERENT subsystem (AppBrowserHost) that a poll loop with no introspection
 * and thin logging is nearly impossible to diagnose after the fact; this is
 * the same fix applied here from the start instead of after an incident.
 */
interface ChannelStatus {
  enabled: boolean;
  tickCount: number;
  lastTickAtIso: string | null;
  lastTickOutcome: string | null;
  carolineEmail: string | null;
  monitoredGroupCount: number;
  lastError: string | null;
  lastErrorAtIso: string | null;
}

const status: ChannelStatus = {
  enabled: false,
  tickCount: 0,
  lastTickAtIso: null,
  lastTickOutcome: null,
  carolineEmail: null,
  monitoredGroupCount: 0,
  lastError: null,
  lastErrorAtIso: null,
};

export function getRatatoskChannelStatus(): ChannelStatus {
  return { ...status };
}

/**
 * Poll loop watching EVERY group Caroline's own Ratatosk account is a member
 * of (not just one hardcoded owner DM -- per explicit correction, 2026-09-03:
 * "Кэролайн должна мониторить ВСЕ группы в которых состоит" -- Caroline
 * previously only ever watched a single DM with the owner, resolved once and
 * cached; being added to any OTHER group, or the owner's phone using a
 * different/duplicate DM than the one cached in memory, meant new messages
 * there were simply never seen, with no error or symptom beyond silence) for
 * new messages from anyone other than herself, injecting each as a proactive
 * turn into the headless "ratatosk" session (see server.ts, which owns
 * creating that session and passes injectFromOwner as its injectProactive).
 *
 * A complete no-op (checked, logged, does nothing further) until Caroline has
 * her own Ratatosk account -- checked on every tick, not just once at
 * startup, so the channel starts working the moment that becomes true
 * without needing a restart. No longer requires the owner to be logged in on
 * THIS machine either: this channel is about what Caroline's own account
 * sees, independent of that. Every tick logs an unconditional heartbeat
 * (what it saw, what it decided) -- same reasoning as server.ts's
 * checkHang(): better a noisy log than another "I can't tell what happened".
 */
export function startRatatoskOwnerChannel(workspaceDir: string, injectFromOwner: (text: string) => void): void {
  console.error(`[caroline] [ratatosk-channel] starting poll loop (interval=${POLL_INTERVAL_MS}ms)`);

  setInterval(async () => {
    status.tickCount++;
    status.lastTickAtIso = new Date().toISOString();
    const tickLabel = `[ratatosk-channel] tick #${status.tickCount}`;
    try {
      const hasOwn = hasOwnRatatoskAccount(workspaceDir);
      status.enabled = hasOwn;
      if (!hasOwn) {
        status.lastTickOutcome = "skipped (no own Ratatosk account yet)";
        console.error(`[caroline] ${tickLabel}: ${status.lastTickOutcome}`);
        return;
      }
      const carolineEmail = ownRatatoskEmail(workspaceDir)!;
      status.carolineEmail = carolineEmail;

      console.error(`[caroline] ${tickLabel}: minting Caroline's own v2 session (email=${carolineEmail})...`);
      const session = await getOwnV2Session(workspaceDir);

      const conversations = await listConversations(session, carolineEmail);
      status.monitoredGroupCount = conversations.length;
      console.error(`[caroline] ${tickLabel}: monitoring ${conversations.length} group(s)`);

      const cursors = loadCursors(workspaceDir);
      const perGroupNew: { group: RatatoskConversation; texts: string[] }[] = [];
      let totalNew = 0;

      for (const group of conversations) {
        const messages = await getRecentMessages(session, group.groupId, 2);
        if (messages.length === 0) continue;
        const maxTs = Math.max(...messages.map((m) => m.ts));

        // On a genuinely first-ever check of a given group (no cursor entry yet --
        // fresh workspace, or Caroline was just added to this group), treat "already
        // seen" as 24h ago rather than "everything up to right now" -- confirmed live
        // (2026-09-02) as a real bug on the single-group predecessor of this loop: the
        // OLD behavior (seed cursor to maxTs, reply to nothing) silently swallowed
        // real messages that had just arrived, because a brand-new group's very first
        // tick treated them exactly like day-old history. Genuinely old history
        // (predating this group's first check by more than a day) still isn't
        // replayed -- only this 24h window is.
        const lastSeenTs = cursors[group.groupId] ?? (getServerNow() - 24 * 3600_000);
        cursors[group.groupId] = maxTs;

        const newMessages = messages.filter((m) => m.ts > lastSeenTs && m.from?.toLowerCase() !== carolineEmail.toLowerCase());
        if (newMessages.length === 0) continue;
        const texts = newMessages.map((m) => `${m.from ?? "unknown"}: ${m.text ?? ""}`).filter((t) => t.trim().length > 0);
        if (texts.length === 0) continue;
        perGroupNew.push({ group, texts });
        totalNew += texts.length;
      }
      saveCursors(workspaceDir, cursors);

      if (perGroupNew.length === 0) {
        status.lastTickOutcome = "no new messages in any monitored group since last cursor";
        console.error(`[caroline] ${tickLabel}: ${status.lastTickOutcome}`);
        return;
      }

      status.lastTickOutcome = `${totalNew} new message(s) across ${perGroupNew.length} group(s), injecting into headless session`;
      console.error(`[caroline] ${tickLabel}: ${status.lastTickOutcome}`);
      // Every group's exact groupId is spelled out right next to its own messages --
      // confirmed live (2026-09-03) as a real bug (on the single-group predecessor of
      // this loop) that a bare "reply there" gives the model no actual value to latch
      // onto, so it can pick a groupId from unrelated prior context/memory instead of
      // the real one, and the reply silently lands in the wrong conversation. With
      // multiple groups possibly pending at once here, that risk is even higher, so
      // this is spelled out once per group, not once for the whole batch.
      const sections = perGroupNew.map(({ group, texts }) =>
        `Group ${JSON.stringify(group.name)} (groupId="${group.groupId}") -- reply here via ratatosk_send_message ` +
          `as:"caroline" groupId:"${group.groupId}" (this exact groupId, not one from earlier in your own history/memory):\n` +
          texts.join("\n"));
      injectFromOwner(
        `[New Ratatosk message(s) since you last checked, across ${perGroupNew.length} group(s) -- reply to each ` +
          `using its OWN groupId shown below, not in any chat window:\n\n${sections.join("\n\n")}]`,
      );
    } catch (err) {
      status.lastError = err instanceof Error ? err.message : String(err);
      status.lastErrorAtIso = new Date().toISOString();
      status.lastTickOutcome = `threw: ${status.lastError}`;
      console.error(`[caroline] ${tickLabel}: threw:`, err);
    }
  }, POLL_INTERVAL_MS);
}

/**
 * Keeps Caroline showing as online in Ratatosk/SquirrelWisdom, everywhere
 * she appears (not scoped to a single group -- presence is keyed only by
 * her own email, see sendPresenceHeartbeat's sw_presence/{email} path), by
 * publishing a fresh heartbeat every PRESENCE_INTERVAL_MS. Deliberately its
 * OWN setInterval, independent of startRatatoskOwnerChannel above -- see
 * PRESENCE_INTERVAL_MS's own comment for why the two must not share a timer.
 * Only gated on Caroline having her own account; unlike the owner-DM poll
 * loop, this does NOT need the owner to be logged in on this machine --
 * presence is Caroline's own status, not a channel to the owner.
 */
export function startRatatoskPresenceHeartbeat(workspaceDir: string): void {
  console.error(`[caroline] [ratatosk-presence] starting heartbeat loop (interval=${PRESENCE_INTERVAL_MS}ms)`);
  let tickCount = 0;

  setInterval(async () => {
    tickCount++;
    try {
      if (!hasOwnRatatoskAccount(workspaceDir)) {
        if (tickCount === 1) console.error(`[caroline] [ratatosk-presence] tick #${tickCount}: skipped (no own account yet)`);
        return;
      }
      const carolineEmail = ownRatatoskEmail(workspaceDir)!;
      const session = await getOwnV2Session(workspaceDir);
      await sendPresenceHeartbeat(session, carolineEmail);
    } catch (err) {
      console.error(`[caroline] [ratatosk-presence] tick #${tickCount}: threw:`, err);
    }
  }, PRESENCE_INTERVAL_MS);
}

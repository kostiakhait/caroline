import { randomUUID } from "node:crypto";
import { fetchWithRetry } from "./httpRetry.js";
import { SQUIRRELWISDOM_API_URL } from "./login.js";

/**
 * Client for Ratatosk (SquirrelWisdom's own messenger, portal/chat.html) --
 * NOT a dedicated bot/API surface (none exists): a caller is just "whoever
 * holds a valid v2 session," same as any browser tab. This wraps the exact
 * same Camerlengo commands chat.js itself uses (confirmed by reading it
 * directly, not guessed): group:create/addMember/get/getUserGroups for
 * conversations, file:read/file:append against day-bucketed JSONL message
 * files for the actual messages. No push -- Ratatosk itself only polls
 * every 4-5s, and so does whatever calls into this (see ratatoskChannel.ts
 * for the one poll loop that exists in this backend).
 *
 * Every function here takes an explicit `session` -- this is deliberate:
 * the exact same functions serve both "act as the owner" (their own
 * SquirrelWisdom session, see login.ts's getV2Session) and "act as
 * Caroline's own account" (ratatoskOwnAccount.ts's own session), nothing
 * here assumes which.
 */

export interface RatatoskMessage {
  id: string;
  ts: number;
  from: string;
  text?: string;
  [key: string]: unknown;
}

// Same shared v2 key portal/chat.js itself sends on every command (its
// module-level __API_KEY -- confirmed by reading that file directly, not
// guessed). NOT a secret in the security sense (it ships in that page's own
// client-side JS to every browser tab); access control is enforced by the
// session + per-command resource ACLs, same as the real web client. Required
// for every command using Api2Dispatcher's default auth="scope" (group:get,
// group:getUserGroups, file:read/write/append/list, ...) -- commands
// declared auth="user_role" (group:create/delete/addMember, ...) only need a
// session and would silently accept a missing key, which is exactly how this
// was missed here originally: confirmed live (2026-09-01) that omitting it
// entirely makes every scope-gated command fail with "Missing, unknown,
// expired or revoked key" while the user_role ones keep working, masking the
// gap until a scope-gated command was actually exercised.
const RATATOSK_API_KEY = "bsqrl2_lkD3dxBD4E4TQyLQJsy5OcDiz63b6h-I3YzGw2SQGKE";

// Best-effort clock-skew correction against the SquirrelWisdom server's own
// clock, kept updated from the standard HTTP Date header every Ratatosk API
// response carries (confirmed present and CORS-exposed -- "Access-Control-
// Expose-Headers: Date" -- via a direct curl against the live endpoint).
// Confirmed live (2026-09-03) as the actual cause of two real problems, both
// reported by the user: Ratatosk message ordering breaking when a device's
// local clock disagrees with others (sendMessage below used to stamp `ts`
// with this machine's own Date.now()), and Caroline appearing offline to
// some viewers because her presence heartbeat's timestamp -- judged against
// only a 15s TTL -- was wrong by however much THIS machine's clock happened
// to be off. getServerNow() below replaces Date.now() wherever Caroline
// stamps a timestamp of her own, removing her own machine's clock as a
// source of that skew entirely. A viewer's own local clock, on the read
// side (see portal/chat.js's updatePresenceDots), is a different codebase
// entirely and out of what this backend can fix.
let serverClockOffsetMs = 0;

function updateServerClockOffset(res: Response): void {
  const dateHeader = res.headers.get("date");
  if (!dateHeader) return;
  const serverMs = Date.parse(dateHeader);
  if (Number.isNaN(serverMs)) return;
  serverClockOffsetMs = serverMs - Date.now();
}

/** Best current estimate of the SquirrelWisdom server's own clock (epoch ms) --
 *  falls back to this machine's own Date.now() (offset 0) until at least one
 *  Ratatosk API response has actually been seen; there's no other source
 *  before that. */
export function getServerNow(): number {
  return Date.now() + serverClockOffsetMs;
}

/** Logs every command sent and its outcome -- the one choke point all
 *  Ratatosk network activity goes through, so this alone gives full
 *  visibility without needing to log at every call site too. Session
 *  tokens are never logged (only which command, and ok/error), same
 *  reasoning as never logging a password. */
async function ratatoskCommand(body: Record<string, unknown>): Promise<any> {
  const { session: _session, ...loggable } = body;
  const started = Date.now();
  console.error(`[caroline] [ratatosk] -> ${JSON.stringify(loggable)}`);
  let data: any;
  try {
    const res = await fetchWithRetry(SQUIRRELWISDOM_API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: RATATOSK_API_KEY, ...body, ".msgid": randomUUID() }),
    });
    updateServerClockOffset(res);
    data = await res.json();
  } catch (err) {
    console.error(`[caroline] [ratatosk] <- ${body.command} threw after ${Date.now() - started}ms:`, err);
    throw err;
  }
  const ok = data?.[".status"] === "ok";
  console.error(`[caroline] [ratatosk] <- ${body.command}: ${ok ? "ok" : `error (${data?.[".reason"] ?? "unknown"})`} (${Date.now() - started}ms)`);
  return data;
}

function isOk(resp: any): boolean {
  return resp?.[".status"] === "ok";
}

/** Base36-timestamp + random suffix -- exact same scheme chat.js's own
 *  newMsgId() uses, so ids Caroline mints look/sort like any other. */
function newMsgId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

/** UTC-ISO date, matching chat.js's own todayStr() (the function the real
 *  send path actually uses for msgFilePath -- some read paths elsewhere in
 *  chat.js use a LOCAL-date variant instead, an existing inconsistency in
 *  Ratatosk itself, not something to replicate here). Server-time-based (see
 *  getServerNow()), not this machine's own clock -- a skewed local clock
 *  near a UTC-midnight boundary could otherwise file a message under the
 *  wrong day's JSONL bucket from the one its own `ts` says it belongs to. */
function todayStr(): string {
  return new Date(getServerNow()).toISOString().split("T")[0];
}

function msgFilePath(groupId: string, date: string): string {
  return `chats/${groupId}/${date}.jsonl`;
}

function parseJSONL(text: string, context?: string): RatatoskMessage[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      try {
        return JSON.parse(l) as RatatoskMessage;
      } catch (err) {
        console.error(`[caroline] [ratatosk] parseJSONL: skipping malformed line${context ? ` (${context})` : ""}:`, err);
        return null;
      }
    })
    .filter((m): m is RatatoskMessage => m !== null);
}

export interface RatatoskConversation {
  groupId: string;
  name: string;
  members: string[];
}

/** `user` (the caller's own email) is required by group:getUserGroups itself
 *  -- confirmed live (2026-09-01) it does NOT infer the caller from
 *  `session` the way most other commands do; portal/chat.js always passes
 *  its own `currentUser` explicitly, and omitting it fails with
 *  '"user" must be provided' even with a valid session. */
export async function listConversations(session: string, user: string): Promise<RatatoskConversation[]> {
  console.error("[caroline] [ratatosk] listConversations: entered");
  const resp = await ratatoskCommand({ command: "group:getUserGroups", user, session });
  if (!isOk(resp)) throw new Error(`group:getUserGroups failed: ${resp?.[".reason"] ?? JSON.stringify(resp)}`);
  const ids: string[] = (resp.groups ?? [])
    .map((item: unknown) => (typeof item === "string" ? item : (item as { group_id?: string })?.group_id))
    .filter((id: unknown): id is string => typeof id === "string" && id.length > 0);
  console.error(`[caroline] [ratatosk] listConversations: ${ids.length} group id(s), fetching each...`);

  const conversations: RatatoskConversation[] = [];
  for (const groupId of ids) {
    const g = await ratatoskCommand({ command: "group:get", group_id: groupId, session });
    if (!isOk(g) || !g.group) {
      console.error(`[caroline] [ratatosk] listConversations: skipping ${groupId} (group:get failed or empty)`);
      continue;
    }
    const members = [...new Set([...(g.group.admins ?? []), ...(g.group.members ?? []), ...(g.group.observers ?? [])])] as string[];
    conversations.push({ groupId, name: g.group.meta?.name || members.join(", "), members });
  }
  console.error(`[caroline] [ratatosk] listConversations: done, ${conversations.length} conversation(s)`);
  return conversations;
}

/** Messages from the last `days` days (today plus however many prior days
 *  are asked for), oldest first -- one file per day, so this is just N
 *  file:read calls concatenated, not a single paged call (Ratatosk itself
 *  has no such endpoint either). */
export async function getRecentMessages(session: string, groupId: string, days = 2): Promise<RatatoskMessage[]> {
  console.error(`[caroline] [ratatosk] getRecentMessages: groupId=${groupId} days=${days}`);
  const messages: RatatoskMessage[] = [];
  const base = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(base);
    d.setUTCDate(d.getUTCDate() - i);
    const dateStr = d.toISOString().split("T")[0];
    const resp = await ratatoskCommand({ command: "file:read", path: msgFilePath(groupId, dateStr), session });
    if (!isOk(resp) || typeof resp.content !== "string") {
      console.error(`[caroline] [ratatosk] getRecentMessages: no content for ${dateStr} (empty day or read failed)`);
      continue;
    }
    // resp.content is base64 (see file:read's response shape) -- confirmed
    // live (2026-09-01) this was NEVER decoded before parsing, so every
    // "line" of the still-base64 blob failed JSON.parse and got silently
    // filtered out by parseJSONL, unconditionally returning 0 messages
    // regardless of what was actually in the file. Never caught before
    // today because this was the first time the owner-DM channel ever had
    // a real message to find (previously indistinguishable from "empty
    // day" -- both looked like 0 results). sendMessage's own base64
    // encoding was always correct; only this read side was missing the
    // matching decode.
    const decoded = Buffer.from(resp.content, "base64").toString("utf-8");
    const parsed = parseJSONL(decoded, `${groupId}/${dateStr}`);
    console.error(`[caroline] [ratatosk] getRecentMessages: ${dateStr} -> ${parsed.length} message(s)`);
    messages.push(...parsed);
  }
  messages.sort((a, b) => a.ts - b.ts);
  console.error(`[caroline] [ratatosk] getRecentMessages: done, ${messages.length} total`);
  return messages;
}

/** Sends as `senderEmail` (must be the account `session` actually belongs
 *  to -- Ratatosk has no separate "send as" concept, the message's `from`
 *  is just a field the client stamps, trusted because the session already
 *  proves the account). */
export async function sendMessage(session: string, groupId: string, senderEmail: string, text: string): Promise<void> {
  const msg: RatatoskMessage = { id: newMsgId(), ts: getServerNow(), from: senderEmail, text };
  console.error(`[caroline] [ratatosk] sendMessage: groupId=${groupId} from=${senderEmail} id=${msg.id} text.length=${text.length}`);
  const content = Buffer.from(JSON.stringify(msg), "utf-8").toString("base64");
  const resp = await ratatoskCommand({
    command: "file:append", path: msgFilePath(groupId, todayStr()), content, session, ".dedup_field": "id",
  });
  if (!isOk(resp)) throw new Error(`file:append failed: ${resp?.[".reason"] ?? JSON.stringify(resp)}`);
  console.error(`[caroline] [ratatosk] sendMessage: sent (id=${msg.id})`);

  // Confirmed live (2026-09-01) as the actual reason a sent message was
  // invisible in the real Ratatosk client despite being correctly persisted:
  // the client's own chat list (loadChatsList/_finaliseChatsList in
  // portal/chat.js) sorts and effectively surfaces conversations by a
  // SEPARATE "sw_lastmsg" variable index (var:set/var:getAll), not by
  // reading the message files directly -- file:append alone never touches
  // it, so a chat sent only this way keeps lastMsgAt=0 forever and sorts to
  // the very bottom of a (here) 39-chat list, indistinguishable from never
  // having happened. Best-effort: a failure here must not fail the send
  // itself, since the message is already correctly persisted either way.
  const bump = await ratatoskCommand({ command: "var:set", path: `sw_lastmsg/${groupId}`, value: String(msg.ts) });
  if (!isOk(bump)) {
    console.error(`[caroline] [ratatosk] sendMessage: WARNING -- sw_lastmsg bump failed (message still sent): ${bump?.[".reason"] ?? JSON.stringify(bump)}`);
  }
}

/** Publishes Caroline's own online-presence heartbeat -- the exact same
 *  sw_presence/{email} var:set mechanism portal/chat.js's own
 *  sendPresenceHeartbeat() uses (PRESENCE_NS = "sw_presence", PRESENCE_TTL_SEC
 *  = 15, value = epoch seconds), confirmed by reading that file directly, not
 *  guessed. Must be called well inside that 15s TTL or Caroline will flicker
 *  offline between heartbeats -- see ratatoskChannel.ts's dedicated 5s
 *  presence timer (deliberately separate from the far-slower 15s owner-DM
 *  poll loop). Best-effort: a failure here is logged but never thrown, same
 *  reasoning as sendMessage's own sw_lastmsg bump above. */
export async function sendPresenceHeartbeat(session: string, email: string): Promise<void> {
  const resp = await ratatoskCommand({
    command: "var:set", path: `sw_presence/${email}`, value: String(Math.floor(getServerNow() / 1000)), session,
  });
  if (!isOk(resp)) {
    console.error(`[caroline] [ratatosk] sendPresenceHeartbeat: WARNING -- failed: ${resp?.[".reason"] ?? JSON.stringify(resp)}`);
  }
}

/** Matches chat.js's own `_looksLikeAutoGroupName()` pattern -- see
 *  findOrCreateDM's own doc comment for why this is the exact discriminator
 *  the real Ratatosk client uses to decide whether a 2-person group can even
 *  be rendered/found as a DM at all. */
function looksLikeAutoGroupName(name: string): boolean {
  return /^Group [0-9A-Z]{8}$/.test(name);
}

/** Finds an existing 2-member group with exactly {selfEmail, otherEmail},
 *  or creates one -- Ratatosk has no dedicated "DM" concept, a DM is just a
 *  group with two members (confirmed in chat.js's own startChatWith). */
export async function findOrCreateDM(session: string, selfEmail: string, otherEmail: string): Promise<string> {
  console.error(`[caroline] [ratatosk] findOrCreateDM: self=${selfEmail} other=${otherEmail}`);
  const conversations = await listConversations(session, selfEmail);
  const matches = conversations.filter((c) => {
    const members = new Set(c.members.map((m) => m.toLowerCase()));
    return members.size === 2 && members.has(selfEmail.toLowerCase()) && members.has(otherEmail.toLowerCase());
  });
  if (matches.length > 0) {
    // Confirmed live (2026-09-03) as a real bug, not hypothetical: TWO 2-member
    // groups existed for the same {selfEmail, otherEmail} pair (an old one
    // predating the auto-name convention below, plus a fresh one the real
    // client created because it couldn't find/render the old one as a DM at
    // all -- see this function's own doc comment for exactly why a
    // wrongly-named group is invisible to the client). Blindly taking
    // matches[0] kept latching onto the OLD, client-invisible group forever
    // -- the owner's messages into the group the phone actually uses were
    // never seen. When more than one match exists, prefer one whose name
    // matches the pattern the real client requires to treat it as a DM at
    // all; only fall back to "just pick one" if none do.
    const existing = matches.length === 1 ? matches[0]
      : matches.find((c) => looksLikeAutoGroupName(c.name)) ?? matches[0];
    if (matches.length > 1) {
      console.error(`[caroline] [ratatosk] findOrCreateDM: WARNING -- ${matches.length} matching DM groups found ` +
        `(${matches.map((c) => `${c.groupId}:${JSON.stringify(c.name)}`).join(", ")}), picking ${existing.groupId}`);
    }
    console.error(`[caroline] [ratatosk] findOrCreateDM: found existing DM (groupId=${existing.groupId})`);
    return existing.groupId;
  }

  const groupId = newMsgId();
  // Confirmed live (2026-09-01): a group created with name:"" and BOTH
  // members in `members` looked fine via the API (group:get returned it
  // correctly, group:getUserGroups even listed it for the owner) but never
  // showed up at all in the real Ratatosk web client's chat list. Root
  // cause found by reading chat.js's own startChatWith directly: (1) it
  // never puts the creator's own email in `members` -- the session holder
  // becomes admin automatically, `members` is only the invitee(s); passing
  // both apparently breaks the client-side "who's the peer" resolution for
  // this 2-person case. (2) it always supplies a name matching
  // generateGroupName()'s exact "Group " + 8 uppercase-alnum-chars shape --
  // the client's own `_looksLikeAutoGroupName()` specifically recognizes
  // that pattern to know "never renamed, show the peer's identity instead"
  // for a 2-person chat; a blank name (defaulted server-side to the raw
  // group_id) doesn't match it and the client apparently can't render the
  // group at all. Replicating both exactly, not guessed -- read straight
  // out of chat.js.
  const autoName = "Group " + Array.from({ length: 8 }, () =>
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"[Math.floor(Math.random() * 36)]).join("");
  console.error(`[caroline] [ratatosk] findOrCreateDM: no existing DM, creating one (groupId=${groupId}, name=${autoName})`);
  const resp = await ratatoskCommand({
    command: "group:create", group_id: groupId, name: autoName, description: "",
    members: [otherEmail], session,
  });
  if (!isOk(resp)) throw new Error(`group:create failed: ${resp?.[".reason"] ?? JSON.stringify(resp)}`);
  console.error(`[caroline] [ratatosk] findOrCreateDM: created (groupId=${groupId})`);
  return groupId;
}

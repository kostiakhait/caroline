// Caroline Messenger sidecar -- see package.json's own description for why
// this exists (fca-unofficial needs Node, Caroline's backend is Python).
// Talks newline-delimited JSON over stdio to app/messenger_channel.py (via
// app/sidecar_process.py), one JSON object per line each direction -- same
// wire shape as companion-apps/whatsapp-sidecar/index.js.
//
// Unlike the WhatsApp sidecar, this process does no linking/login UI of its
// own at all -- by the time it is ever spawned, app/messenger_channel.py has
// already captured a working Facebook session into MESSENGER_APPSTATE_PATH
// (the user logged into facebook.com by hand in Caroline's own embedded
// browser window, including any 2FA/checkpoint -- see messenger_channel.py's
// own doc comment for that full flow and why: fca-unofficial's direct
// email+password login() has no working mid-call verification-code step,
// confirmed by inspecting the installed package itself, so browser-based
// login sidesteps the problem rather than needing one). This file's only
// job is: take that appState, connect, relay messages/commands.
//
// Real API surface below is grounded in the installed package itself, not
// just its docs (confirmed 2026-10-19 by downloading the published tarball
// and reading dist/index.d.ts, docs/DOCS.md and test/fca.test.cjs directly):
//   - require("@dongdev/fca-unofficial") returns the LOGIN FUNCTION itself
//     (dist/cjs.cjs: `module.exports = core` where core IS login, with
//     .login/.createMessengerBot/etc attached as properties) -- NOT an
//     object with a .login property as the entry point. Both work here
//     since core.login === core, but this is called directly as a function,
//     matching test/fca.test.cjs's own "dist/cjs.cjs default export is
//     callable login" test.
//   - Promise form: `const ctx = await fca({appState: [...]}, options)` ->
//     ctx.api (flat, legacy-compatible methods: sendMessage, listenMqtt,
//     getThreadList, etc. -- confirmed live in test/fca.test.cjs's own
//     "optional live login" test) and ctx.userID. appState is an array of
//     cookie objects ({key/name, value, domain, path}, docs: "Recommended"),
//     exactly AppBrowserHost.cs's own /get_cookies response shape -- Python
//     writes it to this path unmodified.
//   - 2FA/"checkpoint": irrelevant to this file -- it only ever sees an
//     appState that was ALREADY a fully-authenticated session by the time
//     Python captured it (the human cleared any checkpoint themselves, in
//     the visible browser, before messenger_channel.py ever wrote this
//     file). "checkpoint"/"checkpoint_282"/"checkpoint_956"/"loginBlocked"
//     are still forwarded below as a best-effort, since Facebook can in
//     principle invalidate a session later and raise these during this
//     sidecar's own lifetime, not just at initial login.
//
// fca-config.json (shipped alongside this file, not auto-generated) locks
// down a real, concerning default discovered by actually running this
// library (2026-10-19, confirmed live in this environment): on first load,
// if that file is missing, the library creates one with checkUpdate.
// install=true -- it would silently `npm install` a newer version of itself
// off the public registry, unreviewed, the first time this sidecar ever
// runs -- and a hardcoded third-party apiServer field (unused by this
// file's own direct-login code path, but shipped pointed at the
// maintainer's own site by default regardless). Both are disabled in the
// shipped file; never delete it and let the library regenerate its own
// defaults here.
//
// Confirmed live (2026-10-19, this environment): `npm install` for this
// package and a credential-less run of this file both behave as commented
// above (clean install, graceful "nothing to log in with" exit) -- a real
// Facebook login itself was NOT exercised (no live/test account available
// in this environment), same gap whatsapp-sidecar's own index.js discloses
// for Baileys.

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const fca = require('@dongdev/fca-unofficial');

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

// Per explicit instruction (2026-10-20), after a real incident: a
// successful-but-empty getThreadList result had nothing logging WHY it
// was empty, so diagnosing it needed a manual standalone reproduction
// instead of just reading caroline.log. Every meaningful operation and
// sub-step in this file now goes through this -- a new "log" message type,
// handled on the Python side (messenger_channel.py's own on_line) as a
// plain log_event, so any future anomaly is already in the log, not
// something that needs re-reproducing by hand. `extra` values that could
// be long (message text, raw API args) are truncated here, not omitted --
// matching this codebase's existing convention elsewhere of logging a
// bounded preview, never full message content, never silence.
const LOG_PREVIEW_CHARS = 300;
function previewValue(v) {
  if (v === undefined || v === null) return v;
  if (typeof v === 'string') return v.length > LOG_PREVIEW_CHARS ? v.slice(0, LOG_PREVIEW_CHARS) + '…' : v;
  try {
    const s = JSON.stringify(v);
    return s.length > LOG_PREVIEW_CHARS ? s.slice(0, LOG_PREVIEW_CHARS) + '…' : v;
  } catch {
    return String(v);
  }
}
function log(message, extra) {
  const out = { type: 'log', message };
  if (extra) {
    for (const [k, v] of Object.entries(extra)) out[k] = previewValue(v);
  }
  emit(out);
}

// Bug fix (2026-10-20), confirmed live: plain `String(e)` on whatever
// fca-unofficial rejects a callback with gave the useless literal string
// "[object Object]" when the rejected value wasn't a real Error instance
// (its own toString never got a chance to run) -- a getThreadList page 2
// failure logged and reported as exactly that, with the real cause lost.
// Used everywhere this file used to just do String(e)/String(err) for a
// log or an outbound error message.
function describeError(e) {
  if (e instanceof Error) return e.stack || e.message || String(e);
  try {
    const s = JSON.stringify(e);
    if (s && s !== '{}') return s;
  } catch {
    // fall through
  }
  return String(e);
}

const appStatePath = process.env.MESSENGER_APPSTATE_PATH || path.join(__dirname, 'appstate.json');

// The user's own explicit choice (2026-10-20) for listChats' per-call
// pagination chunk size -- purely how many threads one getThreadList HTTP
// call asks for, never a cap on the total chat list (see listChats' own
// pagination loop, which keeps going until Facebook itself runs out).
const THREAD_PAGE_SIZE = 20;

let api = null;
let selfID = null;

// Best-effort local chat-name directory, filled in as getThreadList/incoming
// messages mention a thread -- fca-unofficial keeps no persistent chat list
// of its own either, same reasoning as whatsapp-sidecar's own knownChats.
const knownChats = new Map(); // threadID -> name

// Attachment field shapes are typed only as `Loose[]` (i.e. untyped) in the
// package's own .d.ts -- no fixed shape confirmed from docs or types. Best-
// effort: cache the raw attachment objects from each incoming message
// (bounded, FIFO-evicted, same shape as whatsapp-sidecar's pendingMedia) and
// have downloadAttachment try the first URL-like field it finds on them.
const MAX_PENDING_ATTACHMENTS = 200;
const pendingAttachments = new Map(); // ref ("<threadID>:<messageID>") -> raw attachments array

function rememberAttachments(ref, attachments) {
  pendingAttachments.set(ref, attachments);
  while (pendingAttachments.size > MAX_PENDING_ATTACHMENTS) {
    pendingAttachments.delete(pendingAttachments.keys().next().value);
  }
}

function describeAttachments(attachments) {
  if (!attachments || !attachments.length) return null;
  // fca-unofficial's own attachment "type" field (when present) matches the
  // classic facebook-chat-api family's naming -- not independently confirmed
  // against a live message in this environment, so this is a best-effort
  // label, not a guaranteed-correct one, same caveat as the ref lookup below.
  const first = attachments[0];
  const kind = (first && (first.type || first.attachType)) || 'attachment';
  return attachments.length > 1 ? `${attachments.length} attachments (first: ${kind})` : String(kind);
}

function findAttachmentUrl(attachment) {
  return (attachment && (attachment.url || attachment.previewUrl || attachment.downloadUrl || attachment.hiresUrl)) || null;
}

let apiCallSeq = 0;
function apiCall(method, ...args) {
  // Wraps one of ctx.api's callback-style methods as a Promise -- every flat
  // method here accepts an optional trailing callback(err, result) per
  // docs/DOCS.md section 3.1. Every call and its outcome is logged here,
  // in ONE place, so every fca-unofficial call this sidecar ever makes
  // (sendMessage, getThreadList, createNewGroup, all of it) is covered
  // without needing to instrument each command handler separately.
  const callId = ++apiCallSeq;
  log(`apiCall#${callId} ${method} starting`, { args });
  return new Promise((resolve, reject) => {
    if (typeof api[method] !== 'function') {
      const err = new Error(`ctx.api.${method} is not a function in this version of fca-unofficial`);
      log(`apiCall#${callId} ${method} failed: method not found`, { error: describeError(err) });
      reject(err);
      return;
    }
    api[method](...args, (err, result) => {
      if (err) {
        // Preserve the ORIGINAL rejected value's real content (describeError),
        // not a generic `new Error(String(err))` whose .message would already
        // have lost it to "[object Object]" for a non-Error rejection -- the
        // real incident this fixes (2026-10-20): a getThreadList page 2
        // failure logged and surfaced as exactly that useless string.
        const wrapped = err instanceof Error ? err : new Error(describeError(err));
        log(`apiCall#${callId} ${method} failed`, { error: describeError(err) });
        reject(wrapped);
        return;
      }
      log(`apiCall#${callId} ${method} succeeded`, {
        resultType: Array.isArray(result) ? `array(${result.length})` : typeof result,
        result,
      });
      resolve(result);
    });
  });
}

async function login() {
  log('login starting', { appStatePath });
  if (!fs.existsSync(appStatePath)) {
    throw new Error(`no appState at ${appStatePath} -- messenger_login wasn't completed yet, or Python hasn't written this file`);
  }
  const appState = JSON.parse(fs.readFileSync(appStatePath, 'utf8'));
  log('appState loaded', { cookieCount: Array.isArray(appState) ? appState.length : 'not-an-array' });

  // Note (confirmed live, 2026-10-19): the library writes its own human-
  // readable log lines straight to this process's stdout regardless (e.g.
  // "API-LOGIN : Login failed - HTTP 530") -- not JSON, so
  // sidecar_process.py's reader just logs-and-ignores them as unparseable,
  // same tolerance every other sidecar here already has for stray output.
  // `logLevel` in the login() options (documented in the package's own
  // FcaOptions table) is NOT actually honored by this installed version --
  // confirmed live, it logs "Unrecognized option given to setOptions:
  // logLevel" and changes nothing -- so it is deliberately not passed here;
  // a real doc/behavior mismatch, not a mistake in this call.
  const ctx = await fca({ appState }, { listenEvents: true, selfListen: false, autoMarkRead: false, online: false });
  api = ctx.api;
  selfID = ctx.userID || ctx.fbid || null;
  log('fca login resolved', { userID: selfID });

  // Re-save the (possibly refreshed) appState after every successful
  // connect -- Facebook can rotate session cookies over time, and this
  // keeps the file current for the next restart.
  try {
    const state = api.getAppState();
    fs.writeFileSync(appStatePath, JSON.stringify(state));
    log('appState re-saved', { cookieCount: Array.isArray(state) ? state.length : 'not-an-array' });
  } catch (e) {
    emit({ type: 'error', message: `connected but re-saving appState failed: ${e}` });
  }

  emit({ type: 'ready', userID: selfID });
  log('listenMqtt starting');

  // Lifecycle-event forwarding -- guarded since it is not confirmed which
  // object these actually fire on in this version. Confirmed live
  // (2026-10-19, a deliberately bogus appState): "ready" can fire BEFORE
  // Facebook's own later validation catches a broken session -- a fake-but-
  // well-formed cookie set here produced a "ready" event with a userID, and
  // only ~0.3s later did the real rejection surface, as a "loginBlocked"
  // lifecycle event, by which point this process was otherwise sitting in a
  // silently-broken "connected" state (MQTT's auth_error killed listening,
  // but nothing else here would have noticed). These specific events mean
  // the session itself is dead, not a transient hiccup -- emitted as their
  // own "sessionInvalid" type (not a generic "error") specifically so
  // messenger_channel.py can react by discarding the saved appState and
  // forcing this process to stop, rather than leaving it limping along
  // reporting "connected" with no messages ever actually flowing.
  // rateLimit is NOT included here -- that's a transient backoff signal,
  // not proof the session itself is invalid.
  if (typeof api.on === 'function') {
    for (const name of ['checkpoint', 'checkpoint_282', 'checkpoint_956', 'loginBlocked', 'sessionExpired']) {
      api.on(name, (payload) => emit({ type: 'sessionInvalid', event: name, detail: payload || null }));
    }
    api.on('rateLimit', (payload) => emit({ type: 'error', message: `Facebook lifecycle event "rateLimit": ${payload ? JSON.stringify(payload) : '(no detail)'}` }));
  }

  api.listenMqtt((err, event) => {
    if (err) {
      const described = describeError(err);
      log('listenMqtt error', { error: described });
      emit({ type: 'error', message: `listenMqtt error: ${described}` });
      return;
    }
    log('listenMqtt event', { eventType: event && event.type });
    if (!event || (event.type !== 'message' && event.type !== 'message_reply')) return;
    const text = (event.body || '').trim();
    const attachments = event.attachments || [];
    const media = describeAttachments(attachments);
    if (!text && !media) return;
    let attachmentRef;
    if (attachments.length) {
      attachmentRef = `${event.threadID}:${event.messageID}`;
      rememberAttachments(attachmentRef, attachments);
    }
    emit({
      type: 'message',
      threadID: event.threadID,
      senderID: event.senderID,
      messageID: event.messageID,
      fromSelf: event.senderID === selfID,
      text,
      chatName: knownChats.get(event.threadID) || event.threadID,
      media: media || undefined,
      attachmentRef,
    });
  });
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', async (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  log('command received', { command: msg });
  if (!api) {
    log('command rejected: not logged in yet', { cmd: msg.cmd, id: msg.id });
    if (msg.id) emit({ type: 'sendResult', id: msg.id, ok: false, error: 'not logged in yet' });
    return;
  }
  try {
    if (msg.cmd === 'sendMessage') {
      await apiCall('sendMessage', msg.text, msg.to);
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'listChats') {
      // Bug fix (2026-10-20), confirmed live: a hardcoded getThreadList(20,
      // ...) one-shot call meant a real contact (an existing, ordinary
      // conversation, nothing archived/filtered) silently never showed up
      // in messenger_list_chats at all, just because more than 20 OTHER
      // threads ranked ahead of it by Facebook's own recency ordering --
      // not a sync-delay, as it first looked live. Fixed as real pagination
      // instead of a bigger guessed cap (per explicit instruction: no
      // self-picked limit on how many chats this loads -- load every thread
      // that actually exists). THREAD_PAGE_SIZE (the user's own choice, 20)
      // is a pure per-call HTTP chunk size, never a cap on the total result
      // -- the loop below keeps paginating until Facebook itself returns a
      // short/empty page, i.e. there is nothing left to fetch.
      const threads = [];
      let cursor = null;
      let pageNum = 0;
      while (true) {
        pageNum += 1;
        // Bug fix (2026-10-20), confirmed live: page 1 of a real account's
        // getThreadList succeeded with 20 real threads, page 2 (continuing
        // pagination with a real cursor) then rejected -- and since that
        // rejection used to propagate straight out of this whole loop, the
        // ENTIRE already-fetched page 1 was thrown away too, surfacing as
        // one hard failure (or, in a separate real occurrence, an empty
        // array) instead of the 20 real chats this account actually has.
        // A later page failing is now non-fatal: log it clearly and return
        // whatever was successfully gathered so far, same "partial is
        // better than nothing" principle whatsapp_channel.py's own
        // attachment/group code already follows elsewhere in this codebase.
        let page;
        try {
          page = await apiCall('getThreadList', THREAD_PAGE_SIZE, cursor, ['INBOX']);
        } catch (e) {
          log(`listChats page ${pageNum} failed, returning ${threads.length} already-fetched thread(s) instead of failing the whole call`, { error: describeError(e), totalSoFar: threads.length });
          break;
        }
        log(`listChats page ${pageNum}`, { cursor, pageLength: page ? page.length : null });
        if (!page || !page.length) {
          log(`listChats page ${pageNum} empty, stopping pagination`, { totalSoFar: threads.length });
          break;
        }
        threads.push(...page);
        if (page.length < THREAD_PAGE_SIZE) {
          log(`listChats page ${pageNum} short (${page.length} < ${THREAD_PAGE_SIZE}), this was the last page`, { totalSoFar: threads.length });
          break;
        }
        // `timestamp` as the pagination cursor field is the classic
        // facebook-chat-api-family convention (well-established across this
        // whole lineage of forks), not independently confirmed against
        // THIS fork's actual response shape -- same class of caveat as
        // findAttachmentUrl's own best-effort field guesses above; the
        // safety-net check just below (cursor not advancing -> stop) covers
        // the case where this turns out wrong rather than looping forever.
        //
        // Bug fix (2026-10-20), confirmed live: Facebook's own response
        // carries this field as a STRING (large numeric ids/timestamps
        // commonly are, in GraphQL responses), but getThreadList's own
        // cursor argument rejects anything that isn't a real number --
        // confirmed live: "getThreadList: timestamp must be an integer or
        // null". Passed through as a string, page 1 always worked (its own
        // cursor argument is the literal `null`) and EVERY subsequent page
        // always failed -- Number(...) here is the actual fix, not just the
        // fallback (the per-page try/catch above) that made that failure
        // non-fatal.
        const nextCursor = Number(page[page.length - 1].timestamp);
        // Safety net only (never a real-world cap): if the cursor somehow
        // doesn't advance (or isn't a usable number at all), stop rather
        // than loop forever on an unexpected response shape -- better an
        // incomplete list than a hang.
        if (!nextCursor || Number.isNaN(nextCursor) || nextCursor === cursor) {
          log(`listChats page ${pageNum}: cursor did not advance, stopping pagination (safety net)`, { nextCursor, totalSoFar: threads.length });
          break;
        }
        cursor = nextCursor;
      }
      const chats = (threads || []).map((t) => {
        const id = t.threadID || t.id;
        const name = t.name || t.threadName || id;
        if (id) knownChats.set(id, name);
        return { id, name, isGroup: !!t.isGroup };
      });
      log('listChats done', { totalPages: pageNum, totalChats: chats.length });
      emit({ type: 'chats', id: msg.id, chats });
    } else if (msg.cmd === 'setBlock') {
      await apiCall('changeBlockedStatus', msg.userID, msg.block ? 'block' : 'unblock');
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'sendFile') {
      await apiCall('sendMessage', { body: msg.caption || undefined, attachment: fs.createReadStream(msg.path) }, msg.to);
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'downloadAttachment') {
      const attachments = pendingAttachments.get(msg.ref);
      if (!attachments || !attachments.length) {
        emit({ type: 'sendResult', id: msg.id, ok: false, error: `No pending attachment for ref ${msg.ref} -- it may have been evicted, or this isn't an attachment message's ref.` });
        return;
      }
      const url = findAttachmentUrl(attachments[0]);
      if (!url) {
        emit({ type: 'sendResult', id: msg.id, ok: false, error: 'This attachment has no URL field this sidecar recognizes -- its real shape may differ from what was assumed (see this file\'s own header comment).' });
        return;
      }
      const res = await fetch(url);
      if (!res.ok) throw new Error(`fetching attachment URL failed: HTTP ${res.status}`);
      const buffer = Buffer.from(await res.arrayBuffer());
      fs.writeFileSync(msg.savePath, buffer);
      emit({ type: 'sendResult', id: msg.id, ok: true, path: msg.savePath });
    } else if (msg.cmd === 'createGroup') {
      const threadID = await apiCall('createNewGroup', msg.participants, msg.subject);
      emit({ type: 'sendResult', id: msg.id, ok: true, threadID });
    } else if (msg.cmd === 'groupParticipants') {
      if (msg.action === 'add') {
        for (const userID of msg.participants) await apiCall('addUserToGroup', userID, msg.threadID);
      } else if (msg.action === 'remove') {
        for (const userID of msg.participants) await apiCall('removeUserFromGroup', userID, msg.threadID);
      } else if (msg.action === 'promote' || msg.action === 'demote') {
        await apiCall('changeAdminStatus', msg.threadID, msg.participants, msg.action === 'promote');
      } else {
        throw new Error(`unknown groupParticipants action: ${msg.action}`);
      }
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'leaveGroup') {
      if (!selfID) throw new Error('own userID not known yet -- cannot leave');
      await apiCall('removeUserFromGroup', selfID, msg.threadID);
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'deleteGroup') {
      // Messenger has no documented true "delete this group" call (same gap
      // as WhatsApp) -- same deliberate approximation: remove every other
      // participant this account can, then leave, documented as such rather
      // than overclaiming (see messenger_channel.py's own delete_group).
      const info = await apiCall('getThreadInfo', msg.threadID);
      const others = ((info && info.participantIDs) || []).filter((id) => id !== selfID);
      for (const userID of others) await apiCall('removeUserFromGroup', userID, msg.threadID);
      if (selfID) await apiCall('removeUserFromGroup', selfID, msg.threadID);
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else {
      log('command unrecognized', { cmd: msg.cmd, id: msg.id });
    }
  } catch (e) {
    const described = describeError(e);
    log('command failed', { cmd: msg.cmd, id: msg.id, error: described });
    if (msg.id) emit({ type: 'sendResult', id: msg.id, ok: false, error: described });
  }
});

login().catch((e) => {
  const described = describeError(e);
  log('login failed, process exiting', { error: described });
  emit({ type: 'error', message: described });
  process.exit(1);
});

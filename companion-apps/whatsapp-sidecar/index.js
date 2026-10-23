// Caroline WhatsApp sidecar -- see package.json's own description for why
// this exists (Baileys needs Node, Caroline's backend is Python). Talks
// newline-delimited JSON over stdio to app/whatsapp_channel.py (via
// app/sidecar_process.py), one JSON object per line each direction.
//
// Outbound (this process -> Python), one per line on stdout:
//   {"type":"qr","data":"<raw qr string>"}                         -- scan to link
//   {"type":"ready","jid":"<own whatsapp jid>"}                    -- linked and connected
//   {"type":"loggedOut"}                                           -- auth invalidated, needs re-linking
//   {"type":"message","from":"<jid>","fromMe":bool,"text":"...","chatName":"...","media":"photo"?,"mediaRef":"<jid>:<id>"?}
//   {"type":"sendResult","id":"<correlation id>","ok":bool,"error":"..."?,...extra fields per command below}
//   {"type":"chats","id":"<correlation id>","chats":[{"jid":"...","name":"..."}]}
//
// Inbound (Python -> this process), one per line on stdin:
//   {"cmd":"sendMessage","id":"...","to":"<jid>","text":"..."}
//   {"cmd":"listChats","id":"..."}
//   {"cmd":"checkNumber","id":"...","phone":"<digits>"}             -> sendResult{ok,exists,jid}
//   {"cmd":"getInfo","id":"...","jid":"<user or group jid>"}        -> sendResult{ok,info} -- profile, or group with participants
//   {"cmd":"setBlock","id":"...","jid":"...","block":bool}          -> sendResult{ok}
//   {"cmd":"sendFile","id":"...","to":"...","path":"<local path>","caption":"..."?} -> sendResult{ok}
//   {"cmd":"downloadMedia","id":"...","ref":"<jid>:<id>","savePath":"<local path>"} -> sendResult{ok,path}
//   {"cmd":"createGroup","id":"...","subject":"...","participants":["<jid>",...]}   -> sendResult{ok,jid}
//   {"cmd":"groupParticipants","id":"...","jid":"...","participants":[...],"action":"add"|"remove"|"promote"|"demote"} -> sendResult{ok}
//   {"cmd":"leaveGroup","id":"...","jid":"..."}                     -> sendResult{ok}
//   {"cmd":"deleteGroup","id":"...","jid":"..."}                    -> sendResult{ok} -- see deleteGroup below, no true delete exists
//
// 2026-10-11: attachments, groups and block/unblock added the same way as
// the rest of this sidecar -- written from Baileys' real, installed API
// surface (confirmed live against the actual node_modules package this
// time, not just its docs: makeWASocket's method list, groupCreate's and
// downloadMediaMessage's real bodies), syntax- and mock-checked, but still
// not run end-to-end against a live WhatsApp account from this environment.

const readline = require('readline');
const fs = require('fs');
const path = require('path');
const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  downloadMediaMessage,
  getContentType,
  areJidsSameUser,
} = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

const authDir = process.env.WHATSAPP_AUTH_DIR || path.join(__dirname, 'auth');

let sock = null;
// Best-effort local chat directory, built from chats.upsert/contacts.upsert
// events as they arrive -- Baileys itself keeps no persistent chat list
// without the separate (and version-churny) makeInMemoryStore helper, which
// this deliberately avoids depending on.
const knownChats = new Map(); // jid -> name
const knownContacts = new Map(); // jid -> everything WhatsApp told us about the contact (Baileys Contact)

// WhatsApp (unlike Telegram) gives no way to re-fetch an arbitrary message
// by id later -- the only copy of a media message's envelope is the one
// Baileys just handed this process in messages.upsert, so it has to be kept
// around for a later downloadMedia command to use. Bounded and FIFO-evicted
// (Map preserves insertion order) since nothing here ever clears it
// otherwise, and a long-running connection would accumulate forever.
const MAX_PENDING_MEDIA = 200;
const pendingMedia = new Map(); // ref ("<jid>:<messageId>") -> raw WAMessage

function rememberMedia(ref, msg) {
  pendingMedia.set(ref, msg);
  while (pendingMedia.size > MAX_PENDING_MEDIA) {
    pendingMedia.delete(pendingMedia.keys().next().value);
  }
}

// getContentType(message) returns the proto field name present on the
// message (e.g. "imageMessage") -- mapped here to the same short human
// labels telegram_channel.py's _describe_media uses, for a consistent
// proactive-notification shape across both messengers. audioMessage covers
// both a voice note and a regular audio file; the "ptt" flag is what
// Baileys itself uses to tell them apart.
function describeMedia(message) {
  if (!message) return null;
  const type = getContentType(message);
  if (type === 'imageMessage') return 'photo';
  if (type === 'videoMessage') return message.videoMessage && message.videoMessage.ptv ? 'video note' : 'video';
  if (type === 'audioMessage') return message.audioMessage && message.audioMessage.ptt ? 'voice message' : 'audio';
  if (type === 'stickerMessage') return 'sticker';
  if (type === 'documentMessage') {
    const name = message.documentMessage && message.documentMessage.fileName;
    return name ? `file "${name}"` : 'file';
  }
  return null;
}

const MIME_BY_EXT = {
  '.jpg': ['image', 'image/jpeg'], '.jpeg': ['image', 'image/jpeg'], '.png': ['image', 'image/png'],
  '.gif': ['image', 'image/gif'], '.webp': ['image', 'image/webp'],
  '.mp4': ['video', 'video/mp4'], '.mov': ['video', 'video/quicktime'], '.mkv': ['video', 'video/x-matroska'], '.webm': ['video', 'video/webm'],
  '.mp3': ['audio', 'audio/mpeg'], '.m4a': ['audio', 'audio/mp4'], '.ogg': ['audio', 'audio/ogg'], '.wav': ['audio', 'audio/wav'], '.opus': ['audio', 'audio/ogg; codecs=opus'],
};

function guessMedia(filePath) {
  const hit = MIME_BY_EXT[path.extname(filePath).toLowerCase()];
  return hit ? { kind: hit[0], mimetype: hit[1] } : { kind: 'document', mimetype: 'application/octet-stream' };
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  sock = makeWASocket({ auth: state, printQRInTerminal: false, syncFullHistory: false });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      emit({ type: 'qr', data: qr });
    }
    if (connection === 'close') {
      const statusCode = lastDisconnect && lastDisconnect.error instanceof Boom
        ? lastDisconnect.error.output?.statusCode
        : undefined;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      // 2026-10-11 diagnostic (confirmed live: connects, then closes and
      // reconnects every ~5-6s indefinitely, sometimes re-requesting a QR mid-
      // loop) -- statusCode/message were never surfaced before, so there was
      // no way to tell a one-time expected restartRequired (515, Baileys'
      // documented behavior right after a fresh pairing) from a real repeating
      // failure. Surfaced via the existing "error" message type Python already
      // logs (sidecar_reported_error), not a new wire message.
      emit({
        type: 'error',
        message: `connection closed, reconnecting in 5s -- statusCode=${statusCode} loggedOut=${loggedOut} detail=${lastDisconnect && lastDisconnect.error ? String(lastDisconnect.error.message || lastDisconnect.error) : '(none)'}`,
      });
      if (loggedOut) {
        emit({ type: 'loggedOut' });
      } else {
        // Flat retry, same convention as every other channel in Caroline --
        // reconnect after a short pause rather than giving up.
        setTimeout(start, 5000);
      }
    } else if (connection === 'open') {
      emit({ type: 'ready', jid: sock.user ? sock.user.id : null });
    }
  });

  sock.ev.on('chats.upsert', (chats) => {
    for (const c of chats) {
      if (c.id) knownChats.set(c.id, c.name || knownChats.get(c.id) || c.id);
    }
  });

  // Everything WhatsApp told us about each contact (names, lid/phone jids,
  // verified business name), for getInfo -- 2026-10-23, Caroline could not
  // get a phone number or an id out of a conversation.
  const rememberContacts = (contacts) => {
    for (const c of contacts) {
      if (!c.id) continue;
      knownContacts.set(c.id, Object.assign({}, knownContacts.get(c.id), c));
      if (c.name || c.notify) knownChats.set(c.id, c.name || c.notify);
      else if (!knownChats.has(c.id)) knownChats.set(c.id, c.id);
    }
  };
  sock.ev.on('contacts.upsert', rememberContacts);
  sock.ev.on('contacts.update', rememberContacts);

  sock.ev.on('messages.upsert', ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (!msg.message) continue;
      const text =
        msg.message.conversation ||
        (msg.message.extendedTextMessage && msg.message.extendedTextMessage.text) ||
        '';
      // 2026-10-11: a media-only message (no caption) used to be dropped
      // here entirely -- now notified the same way a captioned one is,
      // same decision already made for Telegram, so a bare photo/file is
      // never silently invisible on either messenger.
      const media = describeMedia(msg.message);
      if (!text && !media) continue;
      const jid = msg.key.remoteJid;
      let mediaRef;
      if (media) {
        mediaRef = `${jid}:${msg.key.id}`;
        rememberMedia(mediaRef, msg);
      }
      // knownChats only fills via contacts.upsert, which some senders never
      // trigger at all (seen live: an @lid-addressed jid -- WhatsApp's
      // newer privacy-preserving contact id -- left the chat name unknown
      // even though this was an ongoing conversation). Baileys puts the
      // sender's own self-set WhatsApp display name on msg.pushName
      // directly, no separate sync event needed -- use it whenever
      // knownChats doesn't already have a name, and remember it so the
      // NEXT message from this same jid has it too without needing
      // contacts.upsert to ever fire.
      const pushName = msg.pushName || undefined;
      // Who wrote it: in a group `jid` is the group's, the author is
      // key.participant (2026-10-23: it was never passed on). Their phone
      // number comes as a phone-number jid when WhatsApp gives one -- an
      // @lid id hides it.
      const sender = msg.key.participant || jid;
      const senderPn = msg.key.participantPn || msg.key.senderPn || (sender.endsWith('@s.whatsapp.net') ? sender : undefined);
      if (pushName && !knownChats.has(sender)) knownChats.set(sender, pushName);
      emit({
        type: 'message',
        from: jid,
        fromMe: !!msg.key.fromMe,
        text,
        chatName: knownChats.get(jid) || pushName || jid,
        sender,
        senderName: pushName || knownChats.get(sender) || undefined,
        senderPhone: phoneOf(senderPn),
        media: media || undefined,
        mediaRef,
      });
    }
  });
}

// "+<digits>" from a phone-number jid (123@s.whatsapp.net, 123:4@s.whatsapp.net), else undefined.
function phoneOf(jid) {
  if (!jid || !jid.endsWith('@s.whatsapp.net')) return undefined;
  return '+' + jid.split('@')[0].split(':')[0];
}

// Everything WhatsApp gives about one person or group. Every lookup that
// may be refused (privacy settings, not a business account) is tried on
// its own: what it could not get is left out, never failing the whole answer.
async function getInfo(jid) {
  if (jid.endsWith('@g.us')) {
    const meta = await sock.groupMetadata(jid);
    return {
      kind: 'group', jid: meta.id, subject: meta.subject, description: meta.desc || undefined,
      owner: meta.ownerJid || meta.owner || undefined, created: meta.creation ? new Date(meta.creation * 1000).toISOString() : undefined,
      announceOnly: meta.announce || undefined, isCommunity: meta.isCommunity || undefined, size: meta.participants.length,
      participants: meta.participants.map((p) => {
        const known = knownContacts.get(p.id) || {};
        return {
          id: p.id, phone: phoneOf(p.jid) || phoneOf(p.id), lid: p.lid || (p.id.endsWith('@lid') ? p.id : undefined),
          name: known.name || known.notify || knownChats.get(p.id) || undefined, admin: p.admin || undefined,
        };
      }),
    };
  }
  const known = knownContacts.get(jid) || {};
  const info = {
    kind: 'user', jid, phone: phoneOf(jid) || phoneOf(known.jid), lid: known.lid || (jid.endsWith('@lid') ? jid : undefined),
    savedName: known.name || undefined, ownName: known.notify || undefined, verifiedName: known.verifiedName || undefined,
  };
  try {
    const [found] = await sock.onWhatsApp(jid);
    info.onWhatsApp = !!(found && found.exists);
  } catch (e) { info.onWhatsAppError = String(e); }
  try {
    const statuses = await sock.fetchStatus(jid);
    const status = statuses && statuses[0] && statuses[0].status;
    if (status && status.status) { info.about = status.status; info.aboutSetAt = status.setAt ? new Date(status.setAt).toISOString() : undefined; }
  } catch (e) { info.aboutError = String(e); }
  try {
    const business = await sock.getBusinessProfile(jid);
    if (business) info.business = { description: business.description, category: business.category, email: business.email, website: business.website, address: business.address };
  } catch (e) { /* not a business account */ }
  return info;
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', async (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  if (!sock) {
    if (msg.id) emit({ type: 'sendResult', id: msg.id, ok: false, error: 'not connected yet' });
    return;
  }
  try {
    if (msg.cmd === 'sendMessage') {
      await sock.sendMessage(msg.to, { text: msg.text });
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'listChats') {
      const chats = Array.from(knownChats.entries()).map(([jid, name]) => ({ jid, name }));
      emit({ type: 'chats', id: msg.id, chats });
    } else if (msg.cmd === 'getInfo') {
      emit({ type: 'sendResult', id: msg.id, ok: true, info: await getInfo(msg.jid) });
    } else if (msg.cmd === 'checkNumber') {
      const [result] = await sock.onWhatsApp(msg.phone);
      emit({ type: 'sendResult', id: msg.id, ok: true, exists: !!(result && result.exists), jid: result && result.jid });
    } else if (msg.cmd === 'setBlock') {
      await sock.updateBlockStatus(msg.jid, msg.block ? 'block' : 'unblock');
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'sendFile') {
      const buffer = fs.readFileSync(msg.path);
      const { kind, mimetype } = guessMedia(msg.path);
      const caption = msg.caption || undefined;
      let payload;
      if (kind === 'image') payload = { image: buffer, mimetype, caption };
      else if (kind === 'video') payload = { video: buffer, mimetype, caption };
      else if (kind === 'audio') payload = { audio: buffer, mimetype, ptt: false };
      else payload = { document: buffer, mimetype, fileName: path.basename(msg.path), caption };
      await sock.sendMessage(msg.to, payload);
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'downloadMedia') {
      const cached = pendingMedia.get(msg.ref);
      if (!cached) {
        emit({ type: 'sendResult', id: msg.id, ok: false, error: `No pending attachment for ref ${msg.ref} -- it may have been evicted, or this isn't a media message's ref.` });
      } else {
        const buffer = await downloadMediaMessage(cached, 'buffer', {});
        fs.writeFileSync(msg.savePath, buffer);
        emit({ type: 'sendResult', id: msg.id, ok: true, path: msg.savePath });
      }
    } else if (msg.cmd === 'createGroup') {
      const meta = await sock.groupCreate(msg.subject, msg.participants);
      emit({ type: 'sendResult', id: msg.id, ok: true, jid: meta.id });
    } else if (msg.cmd === 'groupParticipants') {
      await sock.groupParticipantsUpdate(msg.jid, msg.participants, msg.action);
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'leaveGroup') {
      await sock.groupLeave(msg.jid);
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } else if (msg.cmd === 'deleteGroup') {
      // WhatsApp has no real "delete this group" operation -- the closest
      // an admin can do is remove every other participant and then leave,
      // which empties it and makes it inaccessible to everyone, including
      // this account. Documented as a deliberate approximation, not a true
      // delete, in whatsapp_channel.py's own delete_group.
      const meta = await sock.groupMetadata(msg.jid);
      const others = meta.participants.map((p) => p.id).filter((id) => !areJidsSameUser(id, sock.user.id));
      if (others.length) {
        await sock.groupParticipantsUpdate(msg.jid, others, 'remove');
      }
      await sock.groupLeave(msg.jid);
      emit({ type: 'sendResult', id: msg.id, ok: true });
    }
  } catch (e) {
    if (msg.id) emit({ type: 'sendResult', id: msg.id, ok: false, error: String(e) });
  }
});

start().catch((e) => {
  emit({ type: 'error', message: String(e) });
  process.exit(1);
});

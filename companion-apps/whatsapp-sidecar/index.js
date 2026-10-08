// Caroline WhatsApp sidecar -- see package.json's own description for why
// this exists (Baileys needs Node, Caroline's backend is Python). Talks
// newline-delimited JSON over stdio to app/whatsapp_channel.py (via
// app/sidecar_process.py), one JSON object per line each direction.
//
// Outbound (this process -> Python), one per line on stdout:
//   {"type":"qr","data":"<raw qr string>"}                         -- scan to link
//   {"type":"ready","jid":"<own whatsapp jid>"}                    -- linked and connected
//   {"type":"loggedOut"}                                           -- auth invalidated, needs re-linking
//   {"type":"message","from":"<jid>","fromMe":bool,"text":"...","chatName":"..."}
//   {"type":"sendResult","id":"<correlation id>","ok":bool,"error":"..."?}
//   {"type":"chats","id":"<correlation id>","chats":[{"jid":"...","name":"..."}]}
//
// Inbound (Python -> this process), one per line on stdin:
//   {"cmd":"sendMessage","id":"...","to":"<jid>","text":"..."}
//   {"cmd":"listChats","id":"..."}
//
// Best-effort against Baileys' real, version-sensitive API -- written from
// its documented usage patterns, not yet run against a live npm install or
// a real WhatsApp account (no Node runtime available in the dev environment
// this was written in). Confirm connection.update/messages.upsert/
// chats.upsert payload shapes against the actually-installed
// @whiskeysockets/baileys version before relying on this in production.

const readline = require('readline');
const path = require('path');
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys');
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

  sock.ev.on('contacts.upsert', (contacts) => {
    for (const c of contacts) {
      if (c.id) knownChats.set(c.id, c.name || c.notify || knownChats.get(c.id) || c.id);
    }
  });

  sock.ev.on('messages.upsert', ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (!msg.message) continue;
      const text =
        msg.message.conversation ||
        (msg.message.extendedTextMessage && msg.message.extendedTextMessage.text) ||
        '';
      if (!text) continue; // media-only message -- nothing this plugin can usefully surface as text
      const jid = msg.key.remoteJid;
      emit({
        type: 'message',
        from: jid,
        fromMe: !!msg.key.fromMe,
        text,
        chatName: knownChats.get(jid) || jid,
      });
    }
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
  if (!sock) {
    if (msg.id) emit({ type: 'sendResult', id: msg.id, ok: false, error: 'not connected yet' });
    return;
  }
  if (msg.cmd === 'sendMessage') {
    try {
      await sock.sendMessage(msg.to, { text: msg.text });
      emit({ type: 'sendResult', id: msg.id, ok: true });
    } catch (e) {
      emit({ type: 'sendResult', id: msg.id, ok: false, error: String(e) });
    }
  } else if (msg.cmd === 'listChats') {
    const chats = Array.from(knownChats.entries()).map(([jid, name]) => ({ jid, name }));
    emit({ type: 'chats', id: msg.id, chats });
  }
});

start().catch((e) => {
  emit({ type: 'error', message: String(e) });
  process.exit(1);
});

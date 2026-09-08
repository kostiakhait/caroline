import { ImapFlow } from "imapflow";
import { simpleParser, type AddressObject } from "mailparser";
import { writeFile } from "node:fs/promises";
import { getCredentials } from "./session.js";

async function withClient<T>(user: string, fn: (client: ImapFlow) => Promise<T>): Promise<T> {
  const creds = await getCredentials(user);
  const client = new ImapFlow({
    host: creds.imapHost,
    port: creds.imapPort ?? 993,
    secure: true,
    auth: { user: creds.user, pass: creds.password },
    logger: false,
  });
  // Without this, an 'error' event with no listener is an uncaught exception in Node's
  // EventEmitter contract and crashes the whole MCP server process, not just this call.
  client.on("error", (err) => {
    console.error("[mcp-email] IMAP client error:", err);
  });
  await client.connect();
  try {
    return await fn(client);
  } finally {
    await client.logout().catch(() => {});
  }
}

// For mailparser's ParsedMail (used by getMessage/downloadAttachment, via simpleParser).
function addressText(addr: AddressObject | AddressObject[] | undefined): string | undefined {
  if (!addr) return undefined;
  if (Array.isArray(addr)) return addr.map((a) => a.text).join(", ") || undefined;
  return addr.text;
}

// For imapflow's own lighter Envelope shape (used by listMessages, via fetch's envelope field) —
// a different, unrelated address-array shape ({name, address}), not mailparser's AddressObject.
function envelopeAddressText(addrs: { name?: string; address?: string }[] | undefined): string | undefined {
  if (!addrs || addrs.length === 0) return undefined;
  return addrs.map((a) => (a.name ? `${a.name} <${a.address}>` : a.address)).filter(Boolean).join(", ") || undefined;
}

export interface FolderInfo {
  path: string;
  name: string;
  specialUse?: string;
}

/** Appends a raw RFC822 message to a folder (default: the account's Sent folder, auto-detected via specialUse). */
export async function appendMessage(user: string, content: Buffer, folderPath?: string, flags: string[] = ["\\Seen"]): Promise<string> {
  return withClient(user, async (client) => {
    let target = folderPath;
    if (!target) {
      const list = await client.list();
      const sent = list.find((f) => f.specialUse === "\\Sent");
      target = sent?.path ?? "Sent";
    }
    await client.append(target, content, flags);
    return target;
  });
}

export async function listFolders(user: string): Promise<FolderInfo[]> {
  return withClient(user, async (client) => {
    const list = await client.list();
    return list.map((f) => ({ path: f.path, name: f.name, specialUse: f.specialUse }));
  });
}

export interface MessageSummary {
  uid: number;
  from?: string;
  subject?: string;
  date?: string;
  flags: string[];
}

export async function listMessages(
  user: string,
  folder: string,
  opts: { limit?: number; unseenOnly?: boolean; query?: string } = {}
): Promise<MessageSummary[]> {
  return withClient(user, async (client) => {
    const lock = await client.getMailboxLock(folder);
    try {
      const limit = opts.limit ?? 20;
      let uids: number[];
      if (opts.unseenOnly || opts.query) {
        const criteria: Record<string, unknown> = {};
        if (opts.unseenOnly) criteria.seen = false;
        if (opts.query) criteria.or = [{ subject: opts.query }, { from: opts.query }, { body: opts.query }];
        uids = (await client.search(criteria, { uid: true })) || [];
      } else {
        uids = (await client.search({ all: true }, { uid: true })) || [];
      }
      uids = uids.sort((a, b) => b - a).slice(0, limit);
      if (uids.length === 0) return [];

      const byUid = new Map<number, MessageSummary>();
      for await (const msg of client.fetch(uids, { envelope: true, flags: true, uid: true }, { uid: true })) {
        byUid.set(msg.uid, {
          uid: msg.uid,
          from: envelopeAddressText(msg.envelope?.from),
          subject: msg.envelope?.subject,
          date: msg.envelope?.date ? new Date(msg.envelope.date).toISOString() : undefined,
          flags: msg.flags ? Array.from(msg.flags) : [],
        });
      }
      return uids.map((uid) => byUid.get(uid)).filter((m): m is MessageSummary => !!m);
    } finally {
      lock.release();
    }
  });
}

export interface FullMessage {
  uid: number;
  subject?: string;
  from?: string;
  to?: string;
  date?: string;
  text?: string;
  html?: string;
  attachments: { index: number; filename?: string; size: number; contentType: string }[];
}

export async function getMessage(user: string, folder: string, uid: number): Promise<FullMessage> {
  return withClient(user, async (client) => {
    const lock = await client.getMailboxLock(folder);
    try {
      const { content } = await client.download(String(uid), undefined, { uid: true });
      const parsed = await simpleParser(content);
      return {
        uid,
        subject: parsed.subject,
        from: parsed.from?.text,
        to: addressText(parsed.to),
        date: parsed.date?.toISOString(),
        text: parsed.text,
        html: typeof parsed.html === "string" ? parsed.html : undefined,
        attachments: (parsed.attachments || []).map((a, i) => ({
          index: i,
          filename: a.filename,
          size: a.size,
          contentType: a.contentType,
        })),
      };
    } finally {
      lock.release();
    }
  });
}

export async function markFlag(user: string, folder: string, uid: number, flag: string, set: boolean): Promise<void> {
  return withClient(user, async (client) => {
    const lock = await client.getMailboxLock(folder);
    try {
      if (set) await client.messageFlagsAdd(String(uid), [flag], { uid: true });
      else await client.messageFlagsRemove(String(uid), [flag], { uid: true });
    } finally {
      lock.release();
    }
  });
}

export async function moveMessage(user: string, folder: string, uid: number, destFolder: string): Promise<void> {
  return withClient(user, async (client) => {
    const lock = await client.getMailboxLock(folder);
    try {
      await client.messageMove(String(uid), destFolder, { uid: true });
    } finally {
      lock.release();
    }
  });
}

export async function deleteMessage(user: string, folder: string, uid: number): Promise<void> {
  return withClient(user, async (client) => {
    const list = await client.list();
    const trash = list.find((f) => f.specialUse === "\\Trash");
    const lock = await client.getMailboxLock(folder);
    try {
      if (trash && trash.path !== folder) {
        await client.messageMove(String(uid), trash.path, { uid: true });
      } else {
        await client.messageFlagsAdd(String(uid), ["\\Deleted"], { uid: true });
        await client.messageDelete(String(uid), { uid: true });
      }
    } finally {
      lock.release();
    }
  });
}

export async function downloadAttachment(user: string, folder: string, uid: number, attachmentIndex: number, savePath: string): Promise<number> {
  return withClient(user, async (client) => {
    const lock = await client.getMailboxLock(folder);
    try {
      const { content } = await client.download(String(uid), undefined, { uid: true });
      const parsed = await simpleParser(content);
      const attachment = (parsed.attachments || [])[attachmentIndex];
      if (!attachment) throw new Error(`No attachment at index ${attachmentIndex}`);
      await writeFile(savePath, attachment.content);
      return attachment.size;
    } finally {
      lock.release();
    }
  });
}

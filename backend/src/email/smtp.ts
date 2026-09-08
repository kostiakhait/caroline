import nodemailer from "nodemailer";
// @ts-ignore -- nodemailer ships this internal module without its own type declarations
import MailComposer from "nodemailer/lib/mail-composer/index.js";
import { readFile } from "node:fs/promises";
import { basename } from "node:path";
import { getCredentials } from "./session.js";
import { appendMessage } from "./imap.js";

export interface SendAuthOverride {
  user: string;
  password: string;
  smtpHost?: string;
  smtpPort?: number;
}

export interface SendOptions {
  to: string[];
  subject: string;
  from?: string;
  text?: string;
  html?: string;
  cc?: string[];
  bcc?: string[];
  attachments?: { path: string; filename?: string }[];
  inReplyTo?: string;
  references?: string[];
  /** Which saved (vault) account authenticates this send. Required unless `auth` is given
   * instead for a one-off, not-saved account. Every call must name one explicitly — there is
   * no default/active account to fall back to. */
  account?: string;
  auth?: SendAuthOverride;
}

export interface SendResult {
  messageId: string;
  savedToSentFolder: string | null; // folder path if saved, null if skipped
}

function composeRaw(mailOptions: Record<string, unknown>): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    new MailComposer(mailOptions).compile().build((err: Error | null, message: Buffer) => {
      if (err) reject(err);
      else resolve(message);
    });
  });
}

export async function sendEmail(opts: SendOptions): Promise<SendResult> {
  if (!opts.account && !opts.auth) {
    throw new Error(
      "email_send requires either `account` (a saved vault account — see email_list_accounts) " +
      "or `auth` (one-off credentials). There is no default account; every send must name one explicitly."
    );
  }
  const savedCreds = opts.account ? await getCredentials(opts.account) : null;

  const user = opts.auth?.user ?? savedCreds?.user;
  const password = opts.auth?.password ?? savedCreds?.password;
  const smtpHost = opts.auth?.smtpHost ?? savedCreds?.smtpHost ?? savedCreds?.imapHost;
  const smtpPort = opts.auth?.smtpPort ?? savedCreds?.smtpPort ?? 587;

  if (!user || !password || !smtpHost) {
    throw new Error(
      "No SMTP account available. Either pass `account` (a saved vault account), or a full `auth` override ({ user, password, smtpHost })."
    );
  }

  const attachments = opts.attachments
    ? await Promise.all(
        opts.attachments.map(async (a) => ({
          filename: a.filename ?? basename(a.path),
          content: await readFile(a.path),
        }))
      )
    : undefined;

  const from = opts.from ?? user;
  const mailOptions = {
    from,
    to: opts.to.join(", "),
    cc: opts.cc?.join(", "),
    bcc: opts.bcc?.join(", "),
    subject: opts.subject,
    text: opts.text,
    html: opts.html,
    attachments,
    inReplyTo: opts.inReplyTo,
    references: opts.references?.join(" "),
  };

  // Build the exact MIME bytes once (Bcc is correctly present in the envelope below but
  // deliberately omitted from these headers, matching normal mail-client behavior), so the
  // same bytes can be sent via SMTP and archived as-is into the Sent folder via IMAP.
  const raw = await composeRaw(mailOptions);
  const envelopeTo = [...opts.to, ...(opts.cc ?? []), ...(opts.bcc ?? [])];

  const transporter = nodemailer.createTransport({
    host: smtpHost,
    port: smtpPort,
    secure: false, // STARTTLS negotiated on the plaintext-connect port (587), not implicit TLS
    requireTLS: true,
    auth: { user, pass: password },
  });

  const info = await transporter.sendMail({
    envelope: { from: user, to: envelopeTo },
    raw,
  });

  let savedToSentFolder: string | null = null;
  if (opts.account) {
    // Only possible when sending as a saved vault account (`account`) — an `auth` override only
    // supplies SMTP credentials, not a vault entry with an IMAP host, so there's no mailbox to
    // save a copy into.
    try {
      savedToSentFolder = await appendMessage(opts.account, raw);
    } catch (err) {
      console.error(`[caroline] sendMail: appendMessage to Sent folder failed for account ${opts.account} (message was already sent successfully; not failing the call, but no Sent-folder copy exists):`, err);
      savedToSentFolder = null;
    }
  }

  return { messageId: info.messageId, savedToSentFolder };
}

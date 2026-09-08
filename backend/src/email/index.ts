import { z } from "zod";
import { tool, createSdkMcpServer, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";
import { login, listAccounts } from "./session.js";
import {
  createFolder,
  deleteFolder,
  deleteMessage,
  downloadAttachment,
  getMessage,
  listFolders,
  listMessages,
  markFlag,
  moveMessage,
} from "./imap.js";
import { sendEmail } from "./smtp.js";

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function jsonResult(data: unknown) {
  return textResult(JSON.stringify(data, null, 2));
}

const accountParam = z.string().describe(
  'Which saved account performs this operation, e.g. "khait@navlink.net" (see email_list_accounts). ' +
  "Required on every call — there is no default/active account, by design: a prior version of this " +
  "server had a persistent \"active account\" that silently reverted to a stale default across " +
  "process restarts and caused a send to authenticate as the wrong account. Always pass this " +
  "explicitly, every time, even if you just used the same account on the previous call."
);

/**
 * Caroline's own fork of MCP/email (see Caroline/backend/mcp-servers-src/README.md), converted
 * from a separate stdio-transport process into an in-process SDK tool so the "action" calls below
 * (send/delete/move/mark/download) can return immediately and report their real outcome later via
 * a proactive message, instead of blocking the whole turn on a slow IMAP/SMTP round trip.
 *
 * Deliberately NOT applied to the read calls (list_accounts/list_folders/list_messages/
 * get_message/login) -- their result is exactly what Caroline needs to decide her next step, so
 * making those async would force her to wait for a separate follow-up turn just to keep going,
 * which is slower and worse than blocking briefly with the chat UI's own "still working" heartbeat
 * (see chat.js) covering the wait visually.
 */
export function createEmailTool(notify: (text: string) => void): McpServerConfig {
  const emailLogin = tool(
    "email_login",
    "Logs in to a mail account's IMAP/SMTP details. Verifies the credentials by actually connecting, " +
      "then saves them in the vault alongside any other accounts already saved this way. This does NOT " +
      "make the account a default for other calls — every other tool in this server still requires the " +
      "account to be named explicitly each time. If smtpHost/smtpPort are omitted, they default to " +
      "imapHost and 587.",
    {
      user: z.string().describe("Mailbox username (usually the full email address)."),
      password: z.string().describe("Mailbox password."),
      imapHost: z.string().describe("IMAP server hostname, e.g. mail.partners.solutions."),
      imapPort: z.number().int().positive().optional().describe("IMAP port. Defaults to 993 (implicit TLS)."),
      smtpHost: z.string().optional().describe("SMTP server hostname. Defaults to imapHost if omitted."),
      smtpPort: z.number().int().positive().optional().describe("SMTP port. Defaults to 587 (STARTTLS)."),
    },
    async ({ user, password, imapHost, imapPort, smtpHost, smtpPort }) => {
      console.error(`[caroline] [tool:email_login] user=${user} imapHost=${imapHost} imapPort=${imapPort ?? 993} smtpHost=${smtpHost ?? imapHost} smtpPort=${smtpPort ?? 587}`);
      await login({ user, password, imapHost, imapPort, smtpHost, smtpPort });
      console.error(`[caroline] [tool:email_login] user=${user} ok, credentials verified and saved`);
      return textResult(`Saved account ${user} (imap: ${imapHost}:${imapPort ?? 993}, smtp: ${smtpHost ?? imapHost}:${smtpPort ?? 587}). Pass account: "${user}" explicitly on every other call that should use it.`);
    },
  );

  const emailListAccounts = tool(
    "email_list_accounts",
    "Lists every account saved via email_login (email address + IMAP host). Never returns passwords. " +
      "There is no \"active\" account to mark — use the returned addresses as the `account` argument on other calls.",
    {},
    async () => {
      console.error(`[caroline] [tool:email_list_accounts] invoked`);
      return jsonResult(await listAccounts());
    },
  );

  const emailListFolders = tool(
    "email_list_folders",
    "Lists every IMAP folder/mailbox for the given account (INBOX, Sent, Trash, custom folders, etc.).",
    { account: accountParam },
    async ({ account }) => {
      console.error(`[caroline] [tool:email_list_folders] account=${account}`);
      return jsonResult(await listFolders(account));
    },
  );

  const emailListMessages = tool(
    "email_list_messages",
    "Lists lightweight message metadata (uid, from, subject, date, flags) for a folder, newest first. " +
      "Use `query` for a simple subject/from/body substring search, or `unseenOnly` to restrict to unread mail.",
    {
      account: accountParam,
      folder: z.string().default("INBOX").describe('Folder path, e.g. "INBOX". Defaults to INBOX.'),
      limit: z.number().int().positive().max(200).optional().describe("Max messages to return. Defaults to 20."),
      unseenOnly: z.boolean().optional().describe("Only return unread messages."),
      query: z.string().optional().describe("Substring to search for in subject/from/body."),
    },
    async ({ account, folder, limit, unseenOnly, query }) => {
      console.error(`[caroline] [tool:email_list_messages] account=${account} folder=${folder} limit=${limit ?? 20} unseenOnly=${!!unseenOnly} query=${query ?? "n/a"}`);
      return jsonResult(await listMessages(account, folder, { limit, unseenOnly, query }));
    },
  );

  const emailGetMessage = tool(
    "email_get_message",
    "Fetches and parses a single message's full content (subject, from, to, date, text/html body, " +
      "attachment list) by folder + uid.",
    {
      account: accountParam,
      folder: z.string().describe("Folder path the message is in."),
      uid: z.number().int().describe("Message UID (from email_list_messages)."),
    },
    async ({ account, folder, uid }) => {
      console.error(`[caroline] [tool:email_get_message] account=${account} folder=${folder} uid=${uid}`);
      return jsonResult(await getMessage(account, folder, uid));
    },
  );

  const emailMark = tool(
    "email_mark",
    'Adds or removes an IMAP flag on a message, e.g. flag:"\\\\Seen" set:true to mark read, or ' +
      'flag:"\\\\Flagged" for starring. Returns immediately -- you\'ll be told separately if it fails; ' +
      "assume success unless you hear otherwise.",
    {
      account: accountParam,
      folder: z.string().describe("Folder path the message is in."),
      uid: z.number().int().describe("Message UID."),
      flag: z.string().describe('IMAP flag, e.g. "\\\\Seen", "\\\\Flagged", "\\\\Answered".'),
      set: z.boolean().describe("true to add the flag, false to remove it."),
    },
    async ({ account, folder, uid, flag, set }) => {
      console.error(`[caroline] [tool:email_mark] account=${account} folder=${folder} uid=${uid} flag=${flag} set=${set}`);
      markFlag(account, folder, uid, flag, set)
        .then(() => console.error(`[caroline] [tool:email_mark] account=${account} folder=${folder} uid=${uid} flag=${flag} set=${set} ok`))
        .catch((err) => {
          console.error(`[caroline] [tool:email_mark] account=${account} folder=${folder} uid=${uid} failed:`, err);
          notify(`Could not ${set ? "add" : "remove"} flag ${flag} on uid ${uid} in ${folder}: ${err.message || err}`);
        });
      return textResult(`${set ? "Adding" : "Removing"} flag ${flag} on uid ${uid} in ${folder}…`);
    },
  );

  const emailCreateFolder = tool(
    "email_create_folder",
    "Creates an IMAP folder (mailbox). For a nested path (e.g. \"Projects/Foo\"), the parent folder " +
      "usually needs to already exist. Returns immediately -- you'll be told separately if it fails; " +
      "assume success unless you hear otherwise.",
    {
      account: accountParam,
      path: z.string().describe('Folder path to create, e.g. "Projects/Foo".'),
    },
    async ({ account, path }) => {
      console.error(`[caroline] [tool:email_create_folder] account=${account} path=${path}`);
      createFolder(account, path)
        .then(() => console.error(`[caroline] [tool:email_create_folder] account=${account} path=${path} ok`))
        .catch((err) => {
          console.error(`[caroline] [tool:email_create_folder] account=${account} path=${path} failed:`, err);
          notify(`Could not create folder "${path}": ${err.message || err}`);
        });
      return textResult(`Creating folder "${path}"…`);
    },
  );

  const emailDeleteFolder = tool(
    "email_delete_folder",
    "Deletes an IMAP folder (mailbox) and everything in it -- irreversible, there is no trash for the " +
      "folder itself (only for messages moved out of it beforehand). Returns immediately -- you'll be " +
      "told separately if it fails; assume success unless you hear otherwise.",
    {
      account: accountParam,
      path: z.string().describe("Folder path to delete."),
    },
    async ({ account, path }) => {
      console.error(`[caroline] [tool:email_delete_folder] account=${account} path=${path}`);
      deleteFolder(account, path)
        .then(() => console.error(`[caroline] [tool:email_delete_folder] account=${account} path=${path} ok`))
        .catch((err) => {
          console.error(`[caroline] [tool:email_delete_folder] account=${account} path=${path} failed:`, err);
          notify(`Could not delete folder "${path}": ${err.message || err}`);
        });
      return textResult(`Deleting folder "${path}"…`);
    },
  );

  const emailMove = tool(
    "email_move",
    "Moves a message from one folder to another (e.g. archiving, filing into a project folder). " +
      "Returns immediately -- you'll be told separately if it fails; assume success unless you hear otherwise.",
    {
      account: accountParam,
      folder: z.string().describe("Current folder path."),
      uid: z.number().int().describe("Message UID."),
      destFolder: z.string().describe("Destination folder path."),
    },
    async ({ account, folder, uid, destFolder }) => {
      console.error(`[caroline] [tool:email_move] account=${account} folder=${folder} uid=${uid} destFolder=${destFolder}`);
      moveMessage(account, folder, uid, destFolder)
        .then(() => console.error(`[caroline] [tool:email_move] account=${account} uid=${uid} moved to ${destFolder} ok`))
        .catch((err) => {
          console.error(`[caroline] [tool:email_move] account=${account} uid=${uid} failed:`, err);
          notify(`Could not move uid ${uid} from ${folder} to ${destFolder}: ${err.message || err}`);
        });
      return textResult(`Moving uid ${uid} from ${folder} to ${destFolder}…`);
    },
  );

  const emailDelete = tool(
    "email_delete",
    "Deletes a message — moves it to the account's Trash folder if one exists, otherwise flags it " +
      "\\Deleted and expunges it. Returns immediately -- you'll be told separately if it fails; assume " +
      "success unless you hear otherwise.",
    {
      account: accountParam,
      folder: z.string().describe("Folder path the message is in."),
      uid: z.number().int().describe("Message UID."),
    },
    async ({ account, folder, uid }) => {
      console.error(`[caroline] [tool:email_delete] account=${account} folder=${folder} uid=${uid}`);
      deleteMessage(account, folder, uid)
        .then(() => console.error(`[caroline] [tool:email_delete] account=${account} uid=${uid} deleted ok`))
        .catch((err) => {
          console.error(`[caroline] [tool:email_delete] account=${account} uid=${uid} failed:`, err);
          notify(`Could not delete uid ${uid} from ${folder}: ${err.message || err}`);
        });
      return textResult(`Deleting uid ${uid} from ${folder}…`);
    },
  );

  const emailDownloadAttachment = tool(
    "email_download_attachment",
    "Saves one attachment from a message to a local file path, by its index in email_get_message's " +
      "attachment list. Returns immediately -- the real outcome (including the byte count) arrives as a " +
      "separate message once the download finishes; don't assume the file exists until then.",
    {
      account: accountParam,
      folder: z.string().describe("Folder path the message is in."),
      uid: z.number().int().describe("Message UID."),
      attachmentIndex: z.number().int().nonnegative().describe("Index into the attachments array returned by email_get_message."),
      savePath: z.string().describe("Absolute local file path to write the attachment to."),
    },
    async ({ account, folder, uid, attachmentIndex, savePath }) => {
      console.error(`[caroline] [tool:email_download_attachment] account=${account} folder=${folder} uid=${uid} attachmentIndex=${attachmentIndex} savePath=${savePath}`);
      downloadAttachment(account, folder, uid, attachmentIndex, savePath)
        .then((bytes) => {
          console.error(`[caroline] [tool:email_download_attachment] account=${account} uid=${uid} ok, bytes=${bytes}`);
          notify(`Downloaded ${bytes} byte(s) to "${savePath}".`);
        })
        .catch((err) => {
          console.error(`[caroline] [tool:email_download_attachment] account=${account} uid=${uid} failed:`, err);
          notify(`Could not download attachment ${attachmentIndex} from uid ${uid} in ${folder}: ${err.message || err}`);
        });
      return textResult(`Downloading attachment ${attachmentIndex} from uid ${uid} to "${savePath}"…`);
    },
  );

  const emailSend = tool(
    "email_send",
    "Sends an email via SMTP, authenticating as the given `account` (a saved vault account — see " +
      "email_list_accounts). Required on every call, no default. Use `from` to set a different display " +
      "From header (e.g. a vanity address) while still authenticating as `account` — mail servers that " +
      "host multiple domains for the same owner often allow this, but the From header and the " +
      "authenticated account can visibly mismatch to the receiving server if you get this wrong, so " +
      "double-check `account` matches the identity you intend before sending. Use `auth` instead of " +
      "`account` only to authenticate as a genuinely different, not-yet-saved mailbox for this one send. " +
      "Returns immediately -- the real outcome (sent, or the error) arrives as a separate message once " +
      "the SMTP round trip finishes; don't tell the user it was sent until you see that confirmation.",
    {
      account: z
        .string()
        .optional()
        .describe(
          'Saved account to authenticate as, e.g. "khait@navlink.net" (see email_list_accounts). ' +
          "Required unless `auth` is given instead. No default — always pass this explicitly."
        ),
      to: z.array(z.string()).min(1).describe("Recipient email addresses."),
      subject: z.string().describe("Subject line."),
      from: z.string().optional().describe('Display From header, e.g. "Konstantin Khait <k@khait.org>". Defaults to the authenticated account.'),
      text: z.string().optional().describe("Plain-text body."),
      html: z.string().optional().describe("HTML body."),
      cc: z.array(z.string()).optional(),
      bcc: z.array(z.string()).optional(),
      attachments: z
        .array(z.object({ path: z.string().describe("Absolute local file path."), filename: z.string().optional() }))
        .optional()
        .describe("Local files to attach."),
      inReplyTo: z.string().optional().describe("Message-Id header of the message being replied to, for threading."),
      references: z.array(z.string()).optional().describe("Message-Id chain for threading (References header)."),
      auth: z
        .object({
          user: z.string(),
          password: z.string(),
          smtpHost: z.string().optional().describe("Required if not using `account`."),
          smtpPort: z.number().int().positive().optional().describe("Defaults to 587."),
        })
        .optional()
        .describe("One-off credentials for a mailbox not saved via email_login. Use this OR `account`, not both."),
    },
    async ({ account, to, subject, from, text, html, cc, bcc, attachments, inReplyTo, references, auth }) => {
      console.error(`[caroline] [tool:email_send] account=${account ?? auth?.user ?? "n/a"} to=${to.join(", ")} subject=${subject} attachments=${attachments?.length ?? 0}`);
      sendEmail({ account, to, subject, from, text, html, cc, bcc, attachments, inReplyTo, references, auth })
        .then((result) => {
          const sentNote = result.savedToSentFolder
            ? `Saved a copy to "${result.savedToSentFolder}".`
            : auth
              ? "Not saved to Sent (auth override has no vault account to save into)."
              : "Not saved to Sent (could not append — check the account's Sent folder manually).";
          console.error(`[caroline] [tool:email_send] to=${to.join(", ")} subject=${subject} ok, messageId=${result.messageId}`);
          notify(`Email to ${to.join(", ")} (subject: "${subject}") sent as ${account ?? auth?.user}. Message-Id: ${result.messageId}. ${sentNote}`);
        })
        .catch((err) => {
          console.error(`[caroline] [tool:email_send] to=${to.join(", ")} subject=${subject} failed:`, err);
          notify(`Failed to send email to ${to.join(", ")} (subject: "${subject}"): ${err.message || err}`);
        });
      return textResult(`Sending email to ${to.join(", ")} (subject: "${subject}")…`);
    },
  );

  return createSdkMcpServer({
    name: "caroline-email",
    tools: [
      emailLogin, emailListAccounts, emailListFolders, emailListMessages, emailGetMessage,
      emailMark, emailMove, emailDelete, emailDownloadAttachment, emailSend,
      emailCreateFolder, emailDeleteFolder,
    ],
  });
}

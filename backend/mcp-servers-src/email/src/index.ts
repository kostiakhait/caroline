import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { login, listAccounts } from "./session.js";
import {
  deleteMessage,
  downloadAttachment,
  getMessage,
  listFolders,
  listMessages,
  markFlag,
  moveMessage,
} from "./imap.js";
import { sendEmail } from "./smtp.js";

const server = new McpServer({ name: "email", version: "2.0.0" });

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

server.registerTool(
  "email_login",
  {
    title: "Log in to an email account (IMAP/SMTP)",
    description:
      "Logs in to a mail account's IMAP/SMTP details. Verifies the credentials by actually connecting, " +
      "then saves them in the vault alongside any other accounts already saved this way. This does NOT " +
      "make the account a default for other calls — every other tool in this server still requires the " +
      "account to be named explicitly each time. If smtpHost/smtpPort are omitted, they default to " +
      "imapHost and 587.",
    inputSchema: {
      user: z.string().describe("Mailbox username (usually the full email address)."),
      password: z.string().describe("Mailbox password."),
      imapHost: z.string().describe("IMAP server hostname, e.g. mail.partners.solutions."),
      imapPort: z.number().int().positive().optional().describe("IMAP port. Defaults to 993 (implicit TLS)."),
      smtpHost: z.string().optional().describe("SMTP server hostname. Defaults to imapHost if omitted."),
      smtpPort: z.number().int().positive().optional().describe("SMTP port. Defaults to 587 (STARTTLS)."),
    },
  },
  async ({ user, password, imapHost, imapPort, smtpHost, smtpPort }) => {
    await login({ user, password, imapHost, imapPort, smtpHost, smtpPort });
    return textResult(`Saved account ${user} (imap: ${imapHost}:${imapPort ?? 993}, smtp: ${smtpHost ?? imapHost}:${smtpPort ?? 587}). Pass account: "${user}" explicitly on every other call that should use it.`);
  }
);

server.registerTool(
  "email_list_accounts",
  {
    title: "List saved email accounts",
    description: "Lists every account saved via email_login (email address + IMAP host). Never returns passwords. There is no \"active\" account to mark — use the returned addresses as the `account` argument on other calls.",
    inputSchema: {},
  },
  async () => {
    const accounts = await listAccounts();
    return jsonResult(accounts);
  }
);

server.registerTool(
  "email_list_folders",
  {
    title: "List mail folders",
    description: "Lists every IMAP folder/mailbox for the given account (INBOX, Sent, Trash, custom folders, etc.).",
    inputSchema: {
      account: accountParam,
    },
  },
  async ({ account }) => {
    const folders = await listFolders(account);
    return jsonResult(folders);
  }
);

server.registerTool(
  "email_list_messages",
  {
    title: "List messages in a folder",
    description:
      "Lists lightweight message metadata (uid, from, subject, date, flags) for a folder, newest first. " +
      "Use `query` for a simple subject/from/body substring search, or `unseenOnly` to restrict to unread mail.",
    inputSchema: {
      account: accountParam,
      folder: z.string().default("INBOX").describe('Folder path, e.g. "INBOX". Defaults to INBOX.'),
      limit: z.number().int().positive().max(200).optional().describe("Max messages to return. Defaults to 20."),
      unseenOnly: z.boolean().optional().describe("Only return unread messages."),
      query: z.string().optional().describe("Substring to search for in subject/from/body."),
    },
  },
  async ({ account, folder, limit, unseenOnly, query }) => {
    const messages = await listMessages(account, folder, { limit, unseenOnly, query });
    return jsonResult(messages);
  }
);

server.registerTool(
  "email_get_message",
  {
    title: "Get a full message",
    description: "Fetches and parses a single message's full content (subject, from, to, date, text/html body, attachment list) by folder + uid.",
    inputSchema: {
      account: accountParam,
      folder: z.string().describe("Folder path the message is in."),
      uid: z.number().int().describe("Message UID (from email_list_messages)."),
    },
  },
  async ({ account, folder, uid }) => {
    const msg = await getMessage(account, folder, uid);
    return jsonResult(msg);
  }
);

server.registerTool(
  "email_mark",
  {
    title: "Add or remove a message flag",
    description: 'Adds or removes an IMAP flag on a message, e.g. flag:"\\\\Seen" set:true to mark read, or flag:"\\\\Flagged" for starring.',
    inputSchema: {
      account: accountParam,
      folder: z.string().describe("Folder path the message is in."),
      uid: z.number().int().describe("Message UID."),
      flag: z.string().describe('IMAP flag, e.g. "\\\\Seen", "\\\\Flagged", "\\\\Answered".'),
      set: z.boolean().describe("true to add the flag, false to remove it."),
    },
  },
  async ({ account, folder, uid, flag, set }) => {
    await markFlag(account, folder, uid, flag, set);
    return textResult(`${set ? "Added" : "Removed"} flag ${flag} on uid ${uid} in ${folder}.`);
  }
);

server.registerTool(
  "email_move",
  {
    title: "Move a message to another folder",
    description: "Moves a message from one folder to another (e.g. archiving, filing into a project folder).",
    inputSchema: {
      account: accountParam,
      folder: z.string().describe("Current folder path."),
      uid: z.number().int().describe("Message UID."),
      destFolder: z.string().describe("Destination folder path."),
    },
  },
  async ({ account, folder, uid, destFolder }) => {
    await moveMessage(account, folder, uid, destFolder);
    return textResult(`Moved uid ${uid} from ${folder} to ${destFolder}.`);
  }
);

server.registerTool(
  "email_delete",
  {
    title: "Delete a message",
    description: "Deletes a message — moves it to the account's Trash folder if one exists, otherwise flags it \\Deleted and expunges it.",
    inputSchema: {
      account: accountParam,
      folder: z.string().describe("Folder path the message is in."),
      uid: z.number().int().describe("Message UID."),
    },
  },
  async ({ account, folder, uid }) => {
    await deleteMessage(account, folder, uid);
    return textResult(`Deleted uid ${uid} from ${folder}.`);
  }
);

server.registerTool(
  "email_download_attachment",
  {
    title: "Download a message attachment",
    description: "Saves one attachment from a message to a local file path, by its index in email_get_message's attachment list.",
    inputSchema: {
      account: accountParam,
      folder: z.string().describe("Folder path the message is in."),
      uid: z.number().int().describe("Message UID."),
      attachmentIndex: z.number().int().nonnegative().describe("Index into the attachments array returned by email_get_message."),
      savePath: z.string().describe("Absolute local file path to write the attachment to."),
    },
  },
  async ({ account, folder, uid, attachmentIndex, savePath }) => {
    const bytes = await downloadAttachment(account, folder, uid, attachmentIndex, savePath);
    return textResult(`Downloaded ${bytes} byte(s) to "${savePath}".`);
  }
);

server.registerTool(
  "email_send",
  {
    title: "Send an email",
    description:
      "Sends an email via SMTP, authenticating as the given `account` (a saved vault account — see " +
      "email_list_accounts). Required on every call, no default. Use `from` to set a different display " +
      "From header (e.g. a vanity address) while still authenticating as `account` — mail servers that " +
      "host multiple domains for the same owner often allow this, but the From header and the " +
      "authenticated account can visibly mismatch to the receiving server if you get this wrong, so " +
      "double-check `account` matches the identity you intend before sending. Use `auth` instead of " +
      "`account` only to authenticate as a genuinely different, not-yet-saved mailbox for this one send.",
    inputSchema: {
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
  },
  async ({ account, to, subject, from, text, html, cc, bcc, attachments, inReplyTo, references, auth }) => {
    const result = await sendEmail({ account, to, subject, from, text, html, cc, bcc, attachments, inReplyTo, references, auth });
    const sentNote = result.savedToSentFolder
      ? `Saved a copy to "${result.savedToSentFolder}".`
      : auth
      ? "Not saved to Sent (auth override has no vault account to save into)."
      : "Not saved to Sent (could not append — check the account's Sent folder manually).";
    return textResult(`Sent as ${account ?? auth?.user}. Message-Id: ${result.messageId}. ${sentNote}`);
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);

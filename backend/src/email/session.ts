import { ImapFlow } from "imapflow";
import { loadCredentials, saveCredentials, listAccounts as listAccountsConfig, type Credentials } from "./config.js";

// Deliberately no "active account" concept anywhere in this module — no in-memory cache, no
// persisted default pointer on disk. A prior design cached one account in memory and fell back
// to a disk-persisted "last active" pointer across process restarts; that pointer went stale
// silently (e.g. after an unrelated MCP server restart) and caused a send to authenticate as the
// wrong account while displaying a different, correct-looking From header — functionally
// indistinguishable from spoofing to a receiving mail server. Every operation in this server now
// takes an explicit `account` parameter and loads that account's credentials fresh from the
// vault every time. There is nothing to fall back to and nothing to go stale.

async function verifyConnect(creds: Credentials): Promise<void> {
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
  await client.logout();
}

/** Verifies and saves an account's credentials in the vault. Does not make it "active" —
 * there is no such thing. Every subsequent call still names this account explicitly. */
export async function login(creds: Credentials): Promise<void> {
  await verifyConnect(creds);
  await saveCredentials(creds);
}

export async function listAccounts() {
  return listAccountsConfig();
}

/** Loads one account's credentials by its email address, fresh from the vault every time.
 * Every IMAP/SMTP operation in this server calls this with an explicit account argument
 * supplied by the caller for that specific call — never a remembered default. */
export async function getCredentials(user: string): Promise<Credentials> {
  const creds = await loadCredentials(user);
  if (!creds) {
    throw new Error(`No saved account for "${user}". Call email_login first, or check email_list_accounts.`);
  }
  return creds;
}

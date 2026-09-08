import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { fetchWithRetry } from "./httpRetry.js";
import { SQUIRRELWISDOM_API_URL, registerAccountOnly, mintV2Session } from "./login.js";

/**
 * Caroline's OWN SquirrelWisdom/Ratatosk identity -- deliberately separate
 * from login.ts's CREDENTIALS_PATH (the USER's own account, shared with
 * Notes etc.). This is a per-install account: every Caroline instance that
 * opts into the Ratatosk integration (Settings) gets its own unique
 * mailbox/account, not a hardcoded shared identity -- see
 * ensureOwnRatatoskAccount's generation scheme.
 */
function credentialsPath(workspaceDir: string): string {
  return join(workspaceDir, "ratatosk-own-account.json");
}

interface OwnCredentials {
  email: string;
  password: string;
}

function loadOwnCredentials(workspaceDir: string): OwnCredentials | null {
  try {
    return JSON.parse(readFileSync(credentialsPath(workspaceDir), "utf8")) as OwnCredentials;
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code !== "ENOENT") {
      console.error(`[caroline] loadOwnCredentials: read/parse failed (treating as no own account -- a fresh one may get generated, orphaning this one):`, err);
    }
    return null;
  }
}

function saveOwnCredentials(workspaceDir: string, creds: OwnCredentials): void {
  const path = credentialsPath(workspaceDir);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(creds, null, 2), "utf8");
}

export function hasOwnRatatoskAccount(workspaceDir: string): boolean {
  return loadOwnCredentials(workspaceDir) !== null;
}

export function ownRatatoskEmail(workspaceDir: string): string | null {
  return loadOwnCredentials(workspaceDir)?.email ?? null;
}

// Scoped key for the email:create v2 command (see reforce's
// API/Api2EmailCommands.py, cmdV2CreateMailbox) -- same "narrow-grant
// service key" pattern as login.ts's own V2_SERVICE_KEY (user:verify), a
// SEPARATE key rather than reusing that one: least-privilege, a leaked
// email-provisioning key shouldn't also be able to verify arbitrary
// account passwords, and vice versa.
// Minted on the production server directly (Api2Auth.issue_key, scoped to
// "email:create" only, service_name "caroline-email-provisioning",
// non-expiring) -- 2026-08-31.
const EMAIL_CREATE_KEY = "SVdTcM0PwAm1qmZD-ucFUqHpcEiClVGTuJXdHbOEajg";

async function createMailbox(address: string, password: string): Promise<{ ok: true } | { ok: false; error: string }> {
  // Password never logged, same reasoning as ratatosk.ts's ratatoskCommand
  // never logging session tokens.
  console.error(`[caroline] [ratatosk-own-account] createMailbox: address=${address}`);
  const started = Date.now();
  const res = await fetchWithRetry(SQUIRRELWISDOM_API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command: "email:create", key: EMAIL_CREATE_KEY, address, password, ".msgid": randomUUID() }),
  });
  const data: any = await res.json();
  const elapsed = Date.now() - started;
  if (data?.[".status"] !== "ok") {
    const error = String(data?.[".reason"] ?? "Mailbox creation failed");
    console.error(`[caroline] [ratatosk-own-account] createMailbox: failed after ${elapsed}ms: ${error}`);
    return { ok: false, error };
  }
  console.error(`[caroline] [ratatosk-own-account] createMailbox: ok after ${elapsed}ms`);
  return { ok: true };
}

function randomLocalPart(): string {
  return `caroline-${randomBytes(4).toString("hex")}`;
}

function randomPassword(): string {
  return randomBytes(24).toString("base64url");
}

export interface EnsureOwnAccountResult {
  ok: boolean;
  email?: string;
  error?: string;
}

/**
 * Idempotent: if Caroline already has her own account, returns it
 * immediately -- registration (mailbox creation + Ratatosk/SquirrelWisdom
 * account signup) only ever runs once, the first time this is called with
 * no stored credentials yet.
 */
export async function ensureOwnRatatoskAccount(workspaceDir: string): Promise<EnsureOwnAccountResult> {
  const existing = loadOwnCredentials(workspaceDir);
  if (existing) {
    console.error(`[caroline] [ratatosk-own-account] ensureOwnRatatoskAccount: already have one (${existing.email})`);
    return { ok: true, email: existing.email };
  }

  const email = `${randomLocalPart()}@navlink.net`;
  const password = randomPassword();
  console.error(`[caroline] [ratatosk-own-account] ensureOwnRatatoskAccount: no account yet, registering ${email}...`);

  const mailbox = await createMailbox(email, password);
  if (!mailbox.ok) {
    console.error(`[caroline] [ratatosk-own-account] ensureOwnRatatoskAccount: mailbox creation failed: ${mailbox.error}`);
    return { ok: false, error: `Could not create mailbox: ${mailbox.error}` };
  }

  console.error(`[caroline] [ratatosk-own-account] ensureOwnRatatoskAccount: mailbox created, registering SquirrelWisdom account...`);
  const registration = await registerAccountOnly(email, password);
  if (!registration.ok) {
    console.error(`[caroline] [ratatosk-own-account] ensureOwnRatatoskAccount: SquirrelWisdom registration failed: ${registration.error} (mailbox was already created)`);
    return { ok: false, error: `Mailbox created, but SquirrelWisdom registration failed: ${registration.error}` };
  }

  saveOwnCredentials(workspaceDir, { email, password });
  console.error(`[caroline] [ratatosk-own-account] ensureOwnRatatoskAccount: done, registered ${email}`);
  return { ok: true, email };
}

/** Same shape as login.ts's getV2Session(), but for Caroline's OWN account. */
export async function getOwnV2Session(workspaceDir: string): Promise<string> {
  const creds = loadOwnCredentials(workspaceDir);
  if (!creds) throw new Error("Caroline has no Ratatosk account yet -- call ensure_ratatosk_own_account first.");
  console.error(`[caroline] [ratatosk-own-account] getOwnV2Session: minting session for ${creds.email}`);
  return mintV2Session(creds.email, creds.password);
}

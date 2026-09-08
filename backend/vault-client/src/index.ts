import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { callPlugin, genNoteId, verifyPassword, SessionExpiredError, InvalidCredentialsError } from "./api.js";

// Every secret is stored as one note per service, in this folder, titled "vault:<service>"
// (title = first line of the note's text) with the JSON payload as the rest of the body.
const VAULT_FOLDER = "Claude Credentials";
const TITLE_PREFIX = "vault:";

// Reuses the same root credential file MCP/notes already reads/writes via notes_login —
// this is the one local secret every server that wants vault access logs in with. There's
// no way around needing *some* local bootstrap secret (you can't fetch your own login from
// a note you need to be logged in to read), so this piggybacks on the credential the user
// has almost certainly already saved rather than introducing a second local file.
const ROOT_CREDENTIALS_PATH = join(homedir(), ".mcp-notes", "credentials.json");

interface RootCredentials {
  email: string;
  password: string;
}

interface NoteObject {
  text: string;
  updatedAt: number;
  deleted?: boolean;
  folder?: string;
  isFolderMarker?: boolean;
}

async function loadRootCredentials(): Promise<RootCredentials> {
  let raw: string;
  try {
    raw = await readFile(ROOT_CREDENTIALS_PATH, "utf8");
  } catch (err: any) {
    if (err?.code === "ENOENT") {
      throw new Error(
        `No Squirrel Wisdom login found at ${ROOT_CREDENTIALS_PATH}. Call notes_login (MCP/notes) once first — ` +
          "the vault reuses that same saved login to store other services' credentials as notes."
      );
    }
    throw err;
  }
  return JSON.parse(raw) as RootCredentials;
}

// Session tokens die after 24h idle server-side and this process may outlive that, so the
// token itself is never persisted — only cached in memory, with a relogin-and-retry-once on
// expiry, same pattern as MCP/notes's session.ts.
let cachedSession: string | null = null;

async function login(): Promise<string> {
  const { email, password } = await loadRootCredentials();
  try {
    const { session } = await verifyPassword(email, password);
    cachedSession = session;
    return session;
  } catch (err) {
    if (err instanceof InvalidCredentialsError) {
      // Don't cache anything and don't let this look like a transient/server error —
      // the saved password is wrong and only a human can fix it.
      throw new Error(
        `The saved Squirrel Wisdom password for "${email}" was rejected. ` +
          `Call notes_login (MCP/notes) again with the current password to refresh ${ROOT_CREDENTIALS_PATH}, then retry.`
      );
    }
    throw err;
  }
}

async function withSession<T>(fn: (session: string) => Promise<T>): Promise<T> {
  const session = cachedSession ?? (await login());
  try {
    return await fn(session);
  } catch (err) {
    if (!(err instanceof SessionExpiredError)) throw err;
    cachedSession = null;
    const fresh = await login();
    return fn(fresh);
  }
}

interface VaultIndexEntry {
  id: string;
  service: string;
  note: NoteObject;
}

async function readVaultIndex(session: string): Promise<VaultIndexEntry[]> {
  const result = await callPlugin("readIndex", session);
  const index: Record<string, NoteObject> = result?.notes ?? {};
  const entries: VaultIndexEntry[] = [];
  for (const [id, note] of Object.entries(index)) {
    if (note.deleted) continue;
    if ((note.folder ?? "") !== VAULT_FOLDER) continue;
    const title = note.text.split("\n", 1)[0] ?? "";
    if (!title.startsWith(TITLE_PREFIX)) continue;
    entries.push({ id, service: title.slice(TITLE_PREFIX.length), note });
  }
  return entries;
}

/** Returns the stored secret for `service`, or null if nothing is saved for it yet. */
export async function getSecret(service: string): Promise<Record<string, unknown> | null> {
  return withSession(async (session) => {
    const entries = await readVaultIndex(session);
    const match = entries.find((e) => e.service === service);
    if (!match) return null;
    const body = match.note.text.split("\n").slice(1).join("\n");
    return JSON.parse(body);
  });
}

/** Saves (creating or overwriting in place) the secret for `service`. */
export async function setSecret(service: string, data: Record<string, unknown>): Promise<void> {
  await withSession(async (session) => {
    const entries = await readVaultIndex(session);
    const existing = entries.find((e) => e.service === service);
    const id = existing?.id ?? genNoteId();
    const note: NoteObject = {
      text: `${TITLE_PREFIX}${service}\n${JSON.stringify(data)}`,
      updatedAt: Date.now(),
      deleted: false,
      folder: VAULT_FOLDER,
    };
    // Same order as MCP/notes's saveNote: individual file first, then the index entry, so a
    // crash mid-operation leaves the individual file (the source of truth) consistent.
    await callPlugin("writeNoteFile", session, { id, note });
    await callPlugin("patchIndex", session, { entries: { [id]: note } });
  });
}

/** Lists every service name currently stored in the vault — never returns secret values. */
export async function listSecrets(): Promise<string[]> {
  return withSession(async (session) => {
    const entries = await readVaultIndex(session);
    return entries.map((e) => e.service).sort();
  });
}

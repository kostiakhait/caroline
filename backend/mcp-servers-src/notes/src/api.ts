import { createHash, randomInt } from "node:crypto";

const BASE_URL = "https://squirrelwisdom.com";
const APP_KEY = "01Az8nB8mB4cCV";
const ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";

// nginx caps the request body at 64MB; base64 inflates raw bytes by ~33%, so the
// real ceiling for a single attachment's raw file size is ~47MB.
export const MAX_ATTACHMENT_BYTES = 47 * 1024 * 1024;

export interface VerifyPasswordResult {
  session: string;
  user: string;
}

// Thrown when the backend reports the session as expired/invalid, so callers
// (session.ts's withSession) can re-login and retry once instead of failing outright.
export class SessionExpiredError extends Error {}

/**
 * Node's global fetch() (undici) pools keep-alive connections -- in a
 * long-running process (this MCP server can live for hours inside
 * Caroline), a pooled connection can go stale server-side and every
 * subsequent request on it fails with a generic network-level "fetch
 * failed" error, indefinitely, until the process restarts. Confirmed live:
 * a fresh one-off script hitting the exact same endpoint at the same
 * moment worked immediately, while the long-lived server kept failing --
 * the process itself wasn't broken, just its reused connection. A retry
 * opens a fresh connection and succeeds, which is what lets this recover
 * on its own instead of needing Caroline's whole app restarted. Only
 * retries a genuine fetch() throw (DNS/connection-level failure) -- an
 * actual HTTP error response (4xx/5xx) is a real answer from the server,
 * not a stale-connection symptom, so that's surfaced immediately as
 * before, not retried here.
 */
async function fetchWithRetry(url: string, init: RequestInit | undefined, retries = 2): Promise<Response> {
  let lastErr: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      return await fetch(url, init);
    } catch (err) {
      lastErr = err;
      if (attempt < retries) await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
    }
  }
  throw lastErr;
}

async function postJson(body: Record<string, unknown>): Promise<any> {
  const res = await fetchWithRetry(BASE_URL + "/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`Squirrel Wisdom API HTTP ${res.status} for command "${body[".command"]}"`);
  }
  return res.json();
}

export async function verifyPassword(email: string, password: string): Promise<VerifyPasswordResult> {
  const result = await postJson({ ".command": "verifyPassword", key: APP_KEY, path: "/users", user: email, password });
  if (!result?.session) {
    throw new Error(`Login failed for "${email}": ${JSON.stringify(result)}`);
  }
  return { session: result.session, user: result.user ?? email };
}

// Every Notes action goes through the generic plugins:call envelope, authorized by the
// per-request session token (not just at login) — the backend derives hash16 from the
// session server-side, so we never pass it. `query` duplicates `action`: it's a mandatory
// field on the outer envelope, unrelated to which Notes action is being invoked.
export async function callPlugin(action: string, session: string, extra: Record<string, unknown> = {}): Promise<any> {
  const body = { ".command": "plugins:call", plugin: "Notes", query: action, action, key: APP_KEY, session, ...extra };
  const envelope = await postJson(body);
  if (envelope?.[".status"] !== "ok") {
    const reason = envelope?.[".reason"] ?? JSON.stringify(envelope);
    if (typeof reason === "string" && /session/i.test(reason)) {
      throw new SessionExpiredError(reason);
    }
    throw new Error(`Notes plugin action "${action}" failed: ${reason}`);
  }
  return envelope.result;
}

export function hash16(login: string): string {
  return createHash("sha256").update(login, "utf8").digest("hex").slice(0, 16);
}

function randomAlphabetString(length: number): string {
  let out = "";
  for (let i = 0; i < length; i++) {
    out += ID_ALPHABET[randomInt(ID_ALPHABET.length)];
  }
  return out;
}

export function genNoteId(): string {
  return randomAlphabetString(12);
}

export function genAttachmentFilename(): string {
  return randomAlphabetString(16);
}

export function toBase64(data: string | Buffer): string {
  return Buffer.isBuffer(data) ? data.toString("base64") : Buffer.from(data, "utf8").toString("base64");
}

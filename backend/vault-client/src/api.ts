import { randomInt } from "node:crypto";

const BASE_URL = "https://squirrelwisdom.com";
const APP_KEY = "01Az8nB8mB4cCV";
const ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";

export interface VerifyPasswordResult {
  session: string;
  user: string;
}

// Thrown when the backend reports the session as expired/invalid, so callers can
// re-login and retry once instead of failing outright.
export class SessionExpiredError extends Error {}

// Thrown when the *saved password itself* is rejected (as opposed to a network/parse
// failure). Callers must not cache a session or blindly retry on this — the stored
// credential is wrong and needs a human to supply a fresh one via notes_login.
export class InvalidCredentialsError extends Error {}

async function postJson(body: Record<string, unknown>): Promise<any> {
  const res = await fetch(BASE_URL + "/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`Squirrel Wisdom API HTTP ${res.status} for command "${body[".command"]}": ${text.slice(0, 200)}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(
      `Squirrel Wisdom API returned non-JSON for command "${body[".command"]}" (likely an auth/session failure server-side): ${text.slice(0, 200)}`
    );
  }
}

export async function verifyPassword(email: string, password: string): Promise<VerifyPasswordResult> {
  const result = await postJson({ ".command": "verifyPassword", key: APP_KEY, path: "/users", user: email, password });
  if (!result?.session) {
    throw new InvalidCredentialsError(`Login failed for "${email}": ${JSON.stringify(result)}`);
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

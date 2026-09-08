import { loadCredentials, saveCredentials } from "./config.js";
import { hash16, verifyPassword, SessionExpiredError } from "./api.js";

export interface ActiveSession {
  email: string;
  hash16: string;
  session: string;
}

// Session tokens die after 24h of idle time server-side, and this process may live longer
// (or be restarted) — so we never persist a session token to disk. Every process re-logs in
// once, lazily, on first use, with the credentials saved by notes_login. Every action now
// requires this token on every call (not just at login), so withSession() also re-logs in
// and retries once if the backend reports the session expired mid-process.
let active: ActiveSession | null = null;

async function doLogin(email: string, password: string): Promise<ActiveSession> {
  const { session } = await verifyPassword(email, password);
  active = { email, hash16: hash16(email), session };
  return active;
}

export async function login(email: string, password: string): Promise<ActiveSession> {
  const result = await doLogin(email, password);
  await saveCredentials({ email, password });
  return result;
}

export async function ensureSession(): Promise<ActiveSession> {
  if (active) return active;

  const creds = await loadCredentials();
  if (!creds) {
    throw new Error('Not logged in yet. Call notes_login with your Squirrel Wisdom email and password first.');
  }
  return doLogin(creds.email, creds.password);
}

export async function withSession<T>(fn: (ctx: ActiveSession) => Promise<T>): Promise<T> {
  const ctx = await ensureSession();
  try {
    return await fn(ctx);
  } catch (err) {
    if (!(err instanceof SessionExpiredError)) throw err;
    active = null;
    const fresh = await ensureSession();
    return fn(fresh);
  }
}

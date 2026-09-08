import { loadCredentials, saveCredentials } from "./config.js";
import { mintV2Session, SessionExpiredError } from "./api.js";

// Session tokens die after idle timeout server-side, and this process may
// live longer (or be restarted) -- so we never persist a session token to
// disk. Every process re-logs in once, lazily, on first use, with the
// credentials saved by sms_login (or already present from Notes/Caroline
// sharing the same file). Same shape as MCP/notes' session.ts.
let activeSession: string | null = null;

async function doLogin(email: string, password: string): Promise<string> {
  const session = await mintV2Session(email, password);
  activeSession = session;
  return session;
}

export async function login(email: string, password: string): Promise<string> {
  const session = await doLogin(email, password);
  await saveCredentials({ email, password });
  return session;
}

export async function ensureSession(): Promise<string> {
  if (activeSession) return activeSession;
  const creds = await loadCredentials();
  if (!creds) {
    throw new Error("Not logged in yet. Call sms_login with your SquirrelWisdom email and password first.");
  }
  return doLogin(creds.email, creds.password);
}

export async function withSession<T>(fn: (session: string) => Promise<T>): Promise<T> {
  const session = await ensureSession();
  try {
    return await fn(session);
  } catch (err) {
    if (!(err instanceof SessionExpiredError)) throw err;
    activeSession = null;
    const fresh = await ensureSession();
    return fn(fresh);
  }
}

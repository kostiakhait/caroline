import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { randomUUID } from "node:crypto";
import { tool, createSdkMcpServer, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";
import { fetchWithRetry } from "./httpRetry.js";

// Same file/format MCP/notes' own src/config.ts reads (~/.mcp-notes/credentials.json)
// -- Caroline's bundled "caroline-notes" server IS that exact code, spawned as its
// own process under this same OS user account (see workspace.ts's defaultServers),
// so writing here is all it takes for it to log in automatically on its next call.
// Any future SquirrelWisdom-backed capability should read/write this same file
// rather than inventing its own credential store, per the shared-login ask this
// was built for.
const CREDENTIALS_PATH = join(homedir(), ".mcp-notes", "credentials.json");
// Same legacy protocol/app key MCP/notes uses for verifyPassword -- this is the
// same account system, not something Caroline-specific.
const APP_KEY = "01Az8nB8mB4cCV";
const API_URL = "https://www.squirrelwisdom.com/";
export const SQUIRRELWISDOM_APP_KEY = APP_KEY;
export const SQUIRRELWISDOM_API_URL = API_URL;
// Value document:openForEdit/write/etc. get told is the caller's "origin" --
// it isn't actually verified against a real request Origin header, it's
// just checked against a server-side allowlist to pick the base URL used
// for documentUrl/callbackUrl (see reforce's DocumentCommands.py). Caroline
// has no real HTTPS origin of its own (its pages load over file://), so it
// deliberately claims this one, which is both allowlisted and genuinely
// publicly reachable.
export const SQUIRRELWISDOM_ORIGIN = "https://www.squirrelwisdom.com";

interface Credentials {
  email: string;
  password: string;
}

function loadCredentials(): Credentials | null {
  try {
    return JSON.parse(readFileSync(CREDENTIALS_PATH, "utf8")) as Credentials;
  } catch (err) {
    console.error("[caroline] [login] loadCredentials: no stored credentials or unreadable:", (err as Error).message);
    return null;
  }
}

function saveCredentials(creds: Credentials): void {
  mkdirSync(dirname(CREDENTIALS_PATH), { recursive: true });
  writeFileSync(CREDENTIALS_PATH, JSON.stringify(creds, null, 2), "utf8");
}

/**
 * Clears the shared SquirrelWisdom login (this file is also what Caroline's
 * bundled "caroline-notes" server reads, per this file's own header comment
 * -- logging out here logs Notes out too, same account either way). Added
 * so the "no SW login" branch of subscriptionMode's chat-source resolution
 * can actually be exercised/tested without hand-deleting the credentials
 * file -- there was previously no logout path at all, only login.
 */
export function clearCredentials(): void {
  console.error("[caroline] [login] clearCredentials: removing stored SquirrelWisdom credentials");
  try {
    rmSync(CREDENTIALS_PATH, { force: true });
  } catch (err) {
    console.error("[caroline] [login] clearCredentials: rmSync failed (ignored, already gone):", err);
  }
  resetSwAutoPromptFlag();
}

/**
 * Tracks whether a SquirrelWisdom-gated tool has already auto-opened the
 * login/sign-up window once since the last logout (see swGate.ts's
 * requireSwOrPrompt) -- per explicit instruction, the window should open
 * automatically on a gated tool's FIRST refusal, then stay closed on any
 * further refusal until the user explicitly asks to log in (ensure_
 * squirrelwisdom_login, or Settings' "Log in" button, neither throttled by
 * this flag) or logs out and hits a new gated refusal.
 */
let swAutoPromptShown = false;

export function hasAutoPromptedSwLogin(): boolean {
  return swAutoPromptShown;
}

export function markSwAutoPromptShown(): void {
  swAutoPromptShown = true;
}

export function resetSwAutoPromptFlag(): void {
  swAutoPromptShown = false;
}

export function isLoggedIn(): boolean {
  return loadCredentials() !== null;
}

export function loggedInEmail(): string | null {
  return loadCredentials()?.email ?? null;
}

type LoginResult = { ok: true; session: string } | { ok: false; error: string };

async function verifyPassword(email: string, password: string): Promise<LoginResult> {
  console.error(`[caroline] [login] verifyPassword: email=${email}`);
  try {
    const res = await fetchWithRetry(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ".command": "verifyPassword", key: APP_KEY, path: "/users", user: email, password }),
    });
    const data: any = await res.json();
    if (!data?.session) {
      console.error(`[caroline] [login] verifyPassword: email=${email} failed: ${data?.[".reason"] ?? "Invalid email or password."}`);
      return { ok: false, error: String(data?.[".reason"] ?? "Invalid email or password.") };
    }
    console.error(`[caroline] [login] verifyPassword: email=${email} ok`);
    return { ok: true, session: data.session };
  } catch (err) {
    console.error(`[caroline] [login] verifyPassword: email=${email} threw:`, err);
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

export async function verifyAndSaveLogin(email: string, password: string): Promise<LoginResult> {
  const result = await verifyPassword(email, password);
  if (result.ok) {
    saveCredentials({ email, password });
    resetSwAutoPromptFlag();
    console.error(`[caroline] [login] verifyAndSaveLogin: email=${email} credentials saved`);
  }
  return result;
}

/**
 * Self-service registration (Part 2 of the SquirrelWisdom productization
 * plan) -- calls the v2 "user:add" command (auth="public": no key/session
 * needed for self-signup) rather than a legacy endpoint. Same underlying
 * account store as verifyPassword (Auth.Authorizer, shared by both API
 * generations - see Api2UserCommands.py's own docstring), so the account
 * this creates works for both legacy verifyPassword AND v2 user:verify
 * without any extra step.
 */
/**
 * The raw "user:add" call, without touching the shared credentials file --
 * factored out so ratatoskOwnAccount.ts can register Caroline's OWN,
 * separate account the same way without it landing in (or overwriting) the
 * user's own SquirrelWisdom login stored by registerAndSaveLogin below.
 */
export async function registerAccountOnly(email: string, password: string): Promise<LoginResult> {
  console.error(`[caroline] [login] registerAccountOnly: email=${email}`);
  try {
    const res = await fetchWithRetry(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: "user:add", path: "/users", user: email, password }),
    });
    const data: any = await res.json();
    if (data?.[".status"] !== "ok" || !data?.session) {
      console.error(`[caroline] [login] registerAccountOnly: email=${email} failed: ${data?.[".reason"] ?? "Registration failed"}`);
      return { ok: false, error: String(data?.[".reason"] ?? "Registration failed") };
    }
    console.error(`[caroline] [login] registerAccountOnly: email=${email} ok`);
    return { ok: true, session: data.session };
  } catch (err) {
    console.error(`[caroline] [login] registerAccountOnly: email=${email} threw:`, err);
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

export async function registerAndSaveLogin(email: string, password: string): Promise<LoginResult> {
  const result = await registerAccountOnly(email, password);
  if (result.ok) {
    saveCredentials({ email, password });
    resetSwAutoPromptFlag();
    console.error(`[caroline] [login] registerAndSaveLogin: email=${email} credentials saved`);
  }
  return result;
}

/**
 * Session tokens aren't persisted (they die after server-side idle timeout,
 * same reasoning as MCP/notes' own session.ts) -- any SquirrelWisdom-backed
 * feature that needs one (see officeEditor.ts) just calls this fresh each
 * time, which is cheap and avoids tracking expiry itself.
 */
export async function getSession(): Promise<string> {
  const creds = loadCredentials();
  if (!creds) throw new Error("Not logged in to SquirrelWisdom -- call ensure_squirrelwisdom_login first.");
  const result = await verifyPassword(creds.email, creds.password);
  if (!result.ok) throw new Error(`SquirrelWisdom session refresh failed: ${result.error}`);
  console.error(`[caroline] [login] getSession: refreshed legacy session for email=${creds.email}`);
  return result.session;
}

/**
 * Same account, same stored credentials as getSession() above, but mints a
 * Camerlengo API-v2 session (Api2Auth.make_session) via the v2 "user:verify"
 * command instead of legacy verifyPassword's session -- the two are separate
 * stores (see docs/camerlengo.md's legacy-vs-v2 split), and the v2 chat proxy
 * (subscriptionMode.ts) needs the v2 kind specifically. "user:verify" is a
 * scoped (auth="scope") command, hence the key here -- not a secret in the
 * sense the account password is: it only grants "verify this account's own
 * password" and "proxy an already-paid-for Claude call", nothing that acts
 * on another user's data.
 */
const V2_SERVICE_KEY = "fytZDwOTaBo8I173IS2DaY_qgzm0IFvqvnxJGvC5QrE";

/**
 * Mints a v2 session for an ARBITRARY email/password -- factored out of
 * getV2Session() so ratatoskOwnAccount.ts's own separate credentials
 * (Caroline's own Ratatosk account, not the user's) can mint a session the
 * exact same way without this file exposing V2_SERVICE_KEY itself (keeps
 * it private to this module, same trust boundary as before).
 */
export async function mintV2Session(email: string, password: string): Promise<string> {
  console.error(`[caroline] [login] mintV2Session: email=${email}`);
  const res = await fetchWithRetry(API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      command: "user:verify", key: V2_SERVICE_KEY,
      path: "/users", user: email, password,
    }),
  });
  const data: any = await res.json();
  // v2 responses use ".status"/".reason" (dot-prefixed), not "status" -- see
  // Api2Dispatcher.makeResponse()/error().
  if (data?.[".status"] !== "ok" || !data?.session) {
    console.error(`[caroline] [login] mintV2Session: email=${email} failed: ${data?.[".reason"] ?? JSON.stringify(data)}`);
    throw new Error(`SquirrelWisdom v2 session refresh failed: ${data?.[".reason"] ?? JSON.stringify(data)}`);
  }
  console.error(`[caroline] [login] mintV2Session: email=${email} ok`);
  return data.session;
}

export async function getV2Session(): Promise<string> {
  const creds = loadCredentials();
  if (!creds) throw new Error("Not logged in to SquirrelWisdom -- call ensure_squirrelwisdom_login first.");
  return mintV2Session(creds.email, creds.password);
}

/** requestId bookkeeping only, for symmetry with viewer.ts's openRequests -- the
 *  login form doesn't need to describe anything by path once it's done. */
const openRequests = new Set<string>();

export function takeLoginRequest(requestId: string): boolean {
  return openRequests.delete(requestId);
}

type LoginEvent = { type: "open_login"; requestId: string; error?: string; noAiAtAll?: boolean };

/**
 * Opens the native login form, same mechanism ensure_squirrelwisdom_login
 * uses -- factored out so a direct user action (Settings' "Log in" button,
 * see server.ts's "open_login_from_settings" control op) can trigger the
 * exact same flow without going through a chat tool call.
 *
 * noAiAtAll: per explicit instruction (2026-09-07) -- true only when this is
 * runLoop's own chatSource==="none" check (server.ts), the one case where
 * Caroline genuinely cannot talk at all (no Claude account AND no
 * SquirrelWisdom login), as opposed to a single SW-gated feature (Notes,
 * Ratatosk, etc.) being unavailable while chat itself works fine. Lets the
 * native window show honest, context-specific copy instead of always
 * claiming this is "needed for Notes and other features" even when it's
 * actually needed for Caroline to respond at all.
 */
export function openLoginRequest(sendToFrontend: (event: LoginEvent) => void, noAiAtAll = false): string {
  const requestId = randomUUID();
  openRequests.add(requestId);
  console.error(`[caroline] [login] openLoginRequest: requestId=${requestId} noAiAtAll=${noAiAtAll}`);
  sendToFrontend({ type: "open_login", requestId, ...(noAiAtAll ? { noAiAtAll: true } : {}) });
  return requestId;
}

/**
 * Lets Caroline check/establish the user's SquirrelWisdom login -- needed by
 * Notes today, and meant to be reused by other SquirrelWisdom-backed features
 * later without asking the user to log in again (see CREDENTIALS_PATH above).
 *
 * The password NEVER flows through the model's own context: the form opens
 * in Caroline's native viewer window (DocumentViewerWindow's "login" kind)
 * and posts the credentials straight to this backend over the app's own
 * WebView2/WebSocket channel (see server.ts's "login_submit" control op) --
 * the same reasoning as open_in_viewer returning immediately rather than
 * blocking the turn, just applied to "keep secrets out of the transcript"
 * instead of "don't freeze the conversation".
 */
export function createLoginTool(sendToFrontend: (event: LoginEvent) => void): McpServerConfig {
  const ensureLogin = tool(
    "ensure_squirrelwisdom_login",
    "Checks whether the user is logged into their SquirrelWisdom account (needed for Notes and other " +
      "SquirrelWisdom-backed features). If already logged in, returns immediately -- nothing else to do. " +
      "If not, opens a native login form in Caroline's own viewer window and returns immediately; you'll " +
      "be nudged separately once the user logs in or cancels. NEVER ask the user to type their email or " +
      "password into the chat itself -- always use this tool instead.",
    {},
    async () => {
      console.error(`[caroline] [tool:ensure_squirrelwisdom_login] invoked`);
      if (isLoggedIn()) {
        console.error(`[caroline] [tool:ensure_squirrelwisdom_login] already logged in as ${loggedInEmail()}`);
        return { content: [{ type: "text", text: `Already logged in as ${loggedInEmail()}.` }] };
      }
      openLoginRequest(sendToFrontend);
      return {
        content: [{ type: "text", text: "Opened the SquirrelWisdom login form for the user. I'll let you know once they log in or cancel." }],
      };
    },
  );

  return createSdkMcpServer({ name: "caroline-login", tools: [ensureLogin] });
}

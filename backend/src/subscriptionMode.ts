import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { authStatus } from "./control.js";
import { isLoggedIn, loggedInEmail, getSession, getV2Session, SQUIRRELWISDOM_ORIGIN, SQUIRRELWISDOM_API_URL } from "./login.js";
import { fetchWithRetry } from "./httpRetry.js";

// Same scoped key minted for "caroline-desktop" (scopes: user:verify,
// anthropic:messages, wallet:getBalance) -- see login.ts's V2_SERVICE_KEY
// docstring for why a hardcoded scoped key here is fine (narrow grant, not
// the account password).
const SW_SERVICE_KEY = "fytZDwOTaBo8I173IS2DaY_qgzm0IFvqvnxJGvC5QrE";

export type ChatSource = "own-anthropic-oauth" | "own-anthropic-key" | "sw-proxy" | "none";

export interface ResolvedMode {
  chatSource: ChatSource;
  swLoggedIn: boolean;
}

// ---------------------------------------------------------------- settings --
// A manually-pasted ANTHROPIC_API_KEY, as an alternative to the CLI's own
// OAuth login (`claude auth login`) -- same per-workspace JSON-file pattern
// persona.ts uses for persona.json.

interface SubscriptionSettings {
  ownAnthropicApiKey?: string;
}

function settingsPath(workspaceDir: string): string {
  return join(workspaceDir, "subscription.json");
}

function loadSettings(workspaceDir: string): SubscriptionSettings {
  try {
    if (!existsSync(settingsPath(workspaceDir))) return {};
    return JSON.parse(readFileSync(settingsPath(workspaceDir), "utf-8"));
  } catch (err) {
    console.error("[caroline] [subscriptionMode] loadSettings failed, defaulting to {}:", err);
    return {};
  }
}

export function getOwnAnthropicApiKey(workspaceDir: string): string | null {
  return loadSettings(workspaceDir).ownAnthropicApiKey?.trim() || null;
}

export function setOwnAnthropicApiKey(workspaceDir: string, key: string | null): void {
  const s = loadSettings(workspaceDir);
  const trimmed = key?.trim();
  if (trimmed) s.ownAnthropicApiKey = trimmed;
  else delete s.ownAnthropicApiKey;
  writeFileSync(settingsPath(workspaceDir), JSON.stringify(s, null, 2) + "\n", "utf-8");
}

// ------------------------------------------------------------ mode resolve --

/** `claude auth status` prints JSON ({loggedIn, email, subscriptionType,
 *  apiProvider}) -- same shape chat.js already parses for the Settings UI. */
async function hasOwnAnthropicOAuth(cwd: string): Promise<boolean> {
  try {
    const r = await authStatus(cwd);
    if (r.code !== 0) return false;
    const s = JSON.parse(r.stdout || "{}");
    return s?.loggedIn === true;
  } catch (err) {
    console.error("[caroline] [subscriptionMode] hasOwnAnthropicOAuth check failed, treating as not logged in:", err);
    return false;
  }
}

/**
 * Own-Anthropic (OAuth login, then a manually-pasted key) always wins over
 * the SquirrelWisdom proxy when available (confirmed design decision) --
 * someone already paying Anthropic directly is never silently switched onto
 * a metered proxy. Neither available -> "none": server.ts's runLoop checks
 * for exactly this chatSource before creating query() and proactively opens
 * the native login window itself (see requireSwOrPrompt) -- per explicit
 * instruction (2026-09-07), this can't be left to the model to notice and
 * react to, since the model can't run any tool call at all without a chat
 * source to run it with.
 */
export async function resolveMode(workspaceDir: string): Promise<ResolvedMode> {
  const swLoggedIn = isLoggedIn();
  if (await hasOwnAnthropicOAuth(workspaceDir)) {
    console.error(`[caroline] [subscriptionMode] resolveMode: chatSource=own-anthropic-oauth swLoggedIn=${swLoggedIn}`);
    return { chatSource: "own-anthropic-oauth", swLoggedIn };
  }
  if (getOwnAnthropicApiKey(workspaceDir)) {
    console.error(`[caroline] [subscriptionMode] resolveMode: chatSource=own-anthropic-key swLoggedIn=${swLoggedIn}`);
    return { chatSource: "own-anthropic-key", swLoggedIn };
  }
  if (swLoggedIn) {
    console.error(`[caroline] [subscriptionMode] resolveMode: chatSource=sw-proxy swLoggedIn=${swLoggedIn}`);
    return { chatSource: "sw-proxy", swLoggedIn };
  }
  console.error(`[caroline] [subscriptionMode] resolveMode: chatSource=none swLoggedIn=${swLoggedIn}`);
  return { chatSource: "none", swLoggedIn };
}

/**
 * Builds the env override for query()'s Options.env, or undefined to leave
 * the CLI's environment untouched (own-anthropic-oauth: the CLI manages its
 * own OAuth-derived credential, no override needed; none: let the CLI's own
 * request fail, see resolveMode's doc comment).
 *
 * IMPORTANT: per the SDK's own doc comment, Options.env REPLACES the
 * subprocess's environment entirely when provided, it does not merge with
 * process.env automatically -- every branch here that returns a value
 * spreads process.env itself; server.ts must not do it again on top of this.
 */
export async function buildOptionsEnv(workspaceDir: string, mode: ResolvedMode): Promise<Record<string, string> | undefined> {
  if (mode.chatSource === "own-anthropic-key") {
    const key = getOwnAnthropicApiKey(workspaceDir);
    if (!key) return undefined; // race: setting was cleared between resolveMode() and here
    return { ...(process.env as Record<string, string>), ANTHROPIC_API_KEY: key };
  }
  if (mode.chatSource === "sw-proxy") {
    const session = await getV2Session();
    return {
      ...(process.env as Record<string, string>),
      ANTHROPIC_BASE_URL: SQUIRRELWISDOM_ORIGIN,
      ANTHROPIC_API_KEY: `${SW_SERVICE_KEY}.${session}`,
    };
  }
  return undefined;
}

// ------------------------------------------------------- SW account status --

export interface SwStatus {
  loggedIn: boolean;
  email: string | null;
  balancePia: number | null;
  balanceError: string | null;
}

/**
 * Backs the Settings "Account & Billing" section (Part 6) -- separate from
 * resolveMode() because this also needs the actual PIA balance, a real v2
 * call (wallet:getBalance), not just "is SW login configured at all".
 */
export async function getSwStatus(): Promise<SwStatus> {
  const email = loggedInEmail();
  if (!isLoggedIn() || !email) {
    console.error("[caroline] [subscriptionMode] getSwStatus: not logged in");
    return { loggedIn: false, email: null, balancePia: null, balanceError: null };
  }
  try {
    const session = await getV2Session();
    const res = await fetchWithRetry(SQUIRRELWISDOM_API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: "wallet:getBalance", key: SW_SERVICE_KEY, session }),
    });
    const data: any = await res.json();
    if (data?.[".status"] !== "ok") {
      console.error(`[caroline] [subscriptionMode] getSwStatus: balance check failed for ${email}: ${data?.[".reason"] ?? "Balance check failed"}`);
      return { loggedIn: true, email, balancePia: null, balanceError: String(data?.[".reason"] ?? "Balance check failed") };
    }
    const balancePia = Number(data?.balances?.PIA ?? 0);
    console.error(`[caroline] [subscriptionMode] getSwStatus: email=${email} balancePia=${balancePia}`);
    return { loggedIn: true, email, balancePia, balanceError: null };
  } catch (err) {
    console.error(`[caroline] [subscriptionMode] getSwStatus: threw for email=${email}:`, err);
    return { loggedIn: true, email, balancePia: null, balanceError: err instanceof Error ? err.message : String(err) };
  }
}

// ------------------------------------------------------------- top-up/pay --

// Fixed default top-up amount, USD -- a real product would let the user pick
// (or offer a few presets); exact amount/currency choices are a business
// decision, this is a placeholder so "Top up" works end to end.
const DEFAULT_TOPUP_AMOUNT_MINOR = 1000; // $10.00
const DEFAULT_TOPUP_CURRENCY = "USD";

/**
 * Creates a Revolut-hosted top-up checkout session (see reforce's
 * Revolut.create_topup) and returns its checkout_url -- Caroline's "payment"
 * viewer window (Part 5) just navigates a WebView2 straight at this, no
 * custom checkout page of our own to build/host. Uses the LEGACY session
 * (getSession(), not getV2Session()) because /revolut/topups is a legacy-
 * style REST endpoint resolving ?session= via the old Authenticator (see
 * Camerlengo.py's do_POST) - unrelated to the v2 Api2Auth store.
 */
export async function createTopupCheckoutUrl(): Promise<string> {
  console.error(`[caroline] [subscriptionMode] createTopupCheckoutUrl: amount=${DEFAULT_TOPUP_AMOUNT_MINOR} currency=${DEFAULT_TOPUP_CURRENCY}`);
  const session = await getSession();
  const url = `${SQUIRRELWISDOM_ORIGIN}/revolut/topups?session=${encodeURIComponent(session)}`;
  const res = await fetchWithRetry(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ currency: DEFAULT_TOPUP_CURRENCY, amount_minor: DEFAULT_TOPUP_AMOUNT_MINOR, purpose: "wallet_topup" }),
  });
  const data: any = await res.json();
  if (!data?.checkout_url) {
    console.error(`[caroline] [subscriptionMode] createTopupCheckoutUrl: failed: ${JSON.stringify(data)}`);
    throw new Error(`Could not start a top-up: ${JSON.stringify(data)}`);
  }
  console.error(`[caroline] [subscriptionMode] createTopupCheckoutUrl: ok`);
  return data.checkout_url;
}

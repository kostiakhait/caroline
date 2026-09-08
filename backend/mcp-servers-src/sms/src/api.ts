const API_URL = "https://www.squirrelwisdom.com/";

// Grants exactly "sms:*" (sms:send/sms:viewReceived -- the only two commands
// this key actually needs, since sms:setAccount/getAccount/removeAccount are
// auth="user_role", session-only, no key required at all). Same trust
// tier/reasoning as Caroline's own hardcoded SW_SERVICE_KEY: a narrow scope
// grant, not the account password -- safe to embed directly, matching this
// whole codebase's convention (see reforce's Config.py/AI.py for the same
// pattern with other service keys). Minted 2026-09-07, service_name
// "mcp-sms", no expiry.
const SMS_SERVICE_KEY = "sms_zB7vIlt7R_ethw1JCp6IT0cXd3UZsaWf2UGiwoci6FY";

/**
 * Node's global fetch() (undici) pools keep-alive connections -- in a
 * long-running process (this MCP server can live for hours inside
 * Caroline), a pooled connection can go stale server-side. Only retries a
 * genuine fetch() throw (DNS/connection-level failure), not an actual HTTP
 * error response. Same reasoning/shape as MCP/notes' api.ts.
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

// Thrown when the backend reports the session as invalid/expired, so
// session.ts's withSession() can re-mint one and retry once.
export class SessionExpiredError extends Error {}

async function postJson(body: Record<string, unknown>): Promise<any> {
  const res = await fetchWithRetry(API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`SquirrelWisdom API HTTP ${res.status} for command "${body.command}"`);
  }
  return res.json();
}

// v2 responses use ".status"/".reason" (dot-prefixed) -- see
// Api2Dispatcher.makeResponse()/error() on the server side.
async function callV2(command: string, extra: Record<string, unknown> = {}): Promise<any> {
  const envelope = await postJson({ command, ...extra });
  if (envelope?.[".status"] !== "ok") {
    const reason = envelope?.[".reason"] ?? JSON.stringify(envelope);
    if (typeof reason === "string" && /session/i.test(reason)) {
      throw new SessionExpiredError(reason);
    }
    throw new Error(`sms command "${command}" failed: ${reason}`);
  }
  return envelope;
}

/**
 * Mints a v2 session (Api2Auth.make_session, via the "user:verify" command)
 * for an email/password -- same mechanism Caroline's login.ts's
 * mintV2Session uses, reusing the exact same service key (fytZDwOTaBo8I173
 * IS2DaY_qgzm0IFvqvnxJGvC5QrE, scoped "user:verify" only -- "verify this
 * account's own password", nothing that acts on another user's data).
 */
export async function mintV2Session(email: string, password: string): Promise<string> {
  const V2_LOGIN_SERVICE_KEY = "fytZDwOTaBo8I173IS2DaY_qgzm0IFvqvnxJGvC5QrE";
  const data = await postJson({
    command: "user:verify", key: V2_LOGIN_SERVICE_KEY, path: "/users", user: email, password,
  });
  if (data?.[".status"] !== "ok" || !data?.session) {
    throw new Error(`SquirrelWisdom v2 login failed for "${email}": ${data?.[".reason"] ?? JSON.stringify(data)}`);
  }
  return data.session;
}

export interface SmsSendResult {
  total_sent: number | null;
  statuses: Record<string, number> | null;
  messages: Array<{ destination: string; message_id: string; status: string }> | null;
}

export async function smsSend(session: string, destination: string | string[], content: string, sender?: string): Promise<SmsSendResult> {
  const result = await callV2("sms:send", { key: SMS_SERVICE_KEY, session, destination, content, ...(sender ? { sender } : {}) });
  return { total_sent: result.total_sent, statuses: result.statuses, messages: result.messages };
}

export interface SmsReceivedMessage {
  source_address: string;
  destination_address: string;
  content: string;
  timestamp: string;
  message_id: string;
}

export async function smsViewReceived(session: string, startDate?: string, endDate?: string): Promise<SmsReceivedMessage[]> {
  const result = await callV2("sms:viewReceived", {
    key: SMS_SERVICE_KEY, session,
    ...(startDate ? { start_date: startDate } : {}),
    ...(endDate ? { end_date: endDate } : {}),
  });
  return result.messages ?? [];
}

export async function smsSetAccount(session: string, smtp2goApiKey: string, smtp2goSender?: string): Promise<void> {
  await callV2("sms:setAccount", { session, smtp2go_api_key: smtp2goApiKey, ...(smtp2goSender ? { smtp2go_sender: smtp2goSender } : {}) });
}

export interface SmsAccountStatus {
  has_account: boolean;
  sender: string | null;
}

export async function smsGetAccount(session: string): Promise<SmsAccountStatus> {
  const result = await callV2("sms:getAccount", { session });
  return { has_account: result.has_account, sender: result.sender };
}

export async function smsRemoveAccount(session: string): Promise<void> {
  await callV2("sms:removeAccount", { session });
}

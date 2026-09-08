import { getV2Session, SQUIRRELWISDOM_API_URL } from "./login.js";
import { fetchWithRetry } from "./httpRetry.js";

/**
 * Settings' "SMS Account" section -- backs the sms:setAccount/getAccount/
 * removeAccount v2 commands (API/Api2SMSCommands.py). Per explicit
 * instruction, every user sends/receives SMS through THEIR OWN SMTP2GO
 * account (own API key, own number), not a shared one -- this is the paste-
 * your-own-key flow, same shape as subscriptionMode.ts's own-Anthropic-key
 * setting, except the value is stored server-side (Camerlengo's sms_accounts
 * table, keyed by SW login) rather than in a local file, since the caroline-
 * sms MCP tool needs it too and that's a separate process.
 *
 * Unlike getSwStatus's wallet:getBalance call, these are auth="user_role"
 * commands (Api2Dispatcher.py) -- session only, no scoped key needed.
 */

export interface SmsAccountStatus {
  hasAccount: boolean;
  sender: string | null;
  error: string | null;
}

async function callV2(command: string, extra: Record<string, unknown>): Promise<any> {
  const res = await fetchWithRetry(SQUIRRELWISDOM_API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, ...extra }),
  });
  return res.json();
}

export async function getSmsAccountStatus(): Promise<SmsAccountStatus> {
  try {
    const session = await getV2Session();
    const data: any = await callV2("sms:getAccount", { session });
    if (data?.[".status"] !== "ok") {
      console.error(`[caroline] [smsAccount] getSmsAccountStatus failed: ${data?.[".reason"] ?? "unknown"}`);
      return { hasAccount: false, sender: null, error: String(data?.[".reason"] ?? "Status check failed") };
    }
    return { hasAccount: !!data.has_account, sender: data.sender ?? null, error: null };
  } catch (err) {
    console.error("[caroline] [smsAccount] getSmsAccountStatus threw:", err);
    return { hasAccount: false, sender: null, error: err instanceof Error ? err.message : String(err) };
  }
}

export async function setSmsAccount(smtp2goApiKey: string, smtp2goSender: string | null): Promise<{ ok: boolean; error?: string }> {
  try {
    const session = await getV2Session();
    const data: any = await callV2("sms:setAccount", {
      session, smtp2go_api_key: smtp2goApiKey, ...(smtp2goSender ? { smtp2go_sender: smtp2goSender } : {}),
    });
    if (data?.[".status"] !== "ok") {
      console.error(`[caroline] [smsAccount] setSmsAccount failed: ${data?.[".reason"] ?? "unknown"}`);
      return { ok: false, error: String(data?.[".reason"] ?? "Save failed") };
    }
    console.error("[caroline] [smsAccount] setSmsAccount: ok");
    return { ok: true };
  } catch (err) {
    console.error("[caroline] [smsAccount] setSmsAccount threw:", err);
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

export async function removeSmsAccount(): Promise<{ ok: boolean; error?: string }> {
  try {
    const session = await getV2Session();
    const data: any = await callV2("sms:removeAccount", { session });
    if (data?.[".status"] !== "ok") {
      return { ok: false, error: String(data?.[".reason"] ?? "Remove failed") };
    }
    console.error("[caroline] [smsAccount] removeSmsAccount: ok");
    return { ok: true };
  } catch (err) {
    console.error("[caroline] [smsAccount] removeSmsAccount threw:", err);
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

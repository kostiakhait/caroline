import { isLoggedIn, openLoginRequest, hasAutoPromptedSwLogin, markSwAutoPromptShown } from "./login.js";

/**
 * Single source of truth for which tools/features require the user's own
 * SquirrelWisdom account -- not used for runtime enforcement (gating logic
 * differs per call site, e.g. Ratatosk only gates its "owner" identity, not
 * "caroline"'s own separate account), but as documentation and as the thing
 * that drives server.ts's disallowedTools entry for notes_login. Add an
 * entry here whenever a new SW-gated tool is added, even if its own gating
 * still has to be written by hand at the call site.
 */
export interface SwGatedFeature {
  id: string;
  toolNames: string[];
  label: string;
}

export const SW_GATED_FEATURES: SwGatedFeature[] = [
  { id: "consult", toolNames: ["consult_large_model"], label: "Consulting a GPT-5-class model for wording advice" },
  {
    id: "ratatosk-owner",
    toolNames: ["ratatosk_list_conversations", "ratatosk_get_messages", "ratatosk_send_message", "ratatosk_start_chat_with"],
    label: "Ratatosk messaging as the user's own account",
  },
  { id: "office-editor", toolNames: ["open_in_viewer"], label: "Editing Office documents (docx/xlsx/pptx) via OnlyOffice" },
  { id: "notes", toolNames: ["notes_*"], label: "Notes" },
  { id: "sms", toolNames: ["sms_*"], label: "SMS (send/receive via the user's own SMTP2GO account)" },
];

export type SwGateResult = { ok: true } | { ok: false; message: string };

/**
 * The shared gate every SW-gated in-process tool should call before doing
 * its real work. Per explicit instruction: auto-open the native login/
 * sign-up window on the FIRST refusal since the last logout, then on any
 * further refusal just say so without reopening it -- only
 * ensure_squirrelwisdom_login (explicit user request) or Settings' "Log in"
 * button open it after that.
 */
export function requireSwOrPrompt(
  sendToFrontend: (event: { type: "open_login"; requestId: string; error?: string; noAiAtAll?: boolean }) => void,
  noAiAtAll = false
): SwGateResult {
  if (isLoggedIn()) return { ok: true };

  if (!hasAutoPromptedSwLogin()) {
    markSwAutoPromptShown();
    openLoginRequest(sendToFrontend, noAiAtAll);
    return {
      ok: false,
      message:
        "Not available: the user isn't logged into SquirrelWisdom. I've opened the native login/sign-up window for " +
        "them -- tell them, and continue once they log in (you'll be nudged separately). Don't reopen the window " +
        "yourself if this happens again -- only call ensure_squirrelwisdom_login if the user explicitly asks to log in.",
    };
  }

  return {
    ok: false,
    message:
      "Still not available: the user isn't logged into SquirrelWisdom (the login window was already opened once for " +
      "this). Don't reopen it yourself -- only call ensure_squirrelwisdom_login if the user explicitly asks to log " +
      "in, or point them to Settings -> Account & Billing.",
  };
}

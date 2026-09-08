import { z } from "zod";
import { tool, createSdkMcpServer, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";
import { getV2Session, isLoggedIn, loggedInEmail } from "./login.js";
import { ensureOwnRatatoskAccount, getOwnV2Session, hasOwnRatatoskAccount, ownRatatoskEmail } from "./ratatoskOwnAccount.js";
import { listConversations, getRecentMessages, sendMessage, findOrCreateDM } from "./ratatosk.js";
import { requireSwOrPrompt } from "./swGate.js";

type SendLoginEvent = (event: { type: "open_login"; requestId: string; error?: string }) => void;

/**
 * MCP tools for Ratatosk (SquirrelWisdom's messenger) -- see the
 * ratatosk-messenger skill for when/how to use these. Every tool takes
 * `as: "owner" | "caroline"` so the SAME tool set drives both identities:
 * "owner" acts as the user's own SquirrelWisdom session (their messages,
 * their contacts -- broad standing authorization, no per-message
 * confirmation needed, per the user's own explicit sign-off), "caroline"
 * acts as Caroline's own separate Ratatosk account (see
 * ratatoskOwnAccount.ts), if one has been registered.
 */

async function resolveSession(workspaceDir: string, as: "owner" | "caroline", sendToFrontend: SendLoginEvent): Promise<{ session: string; senderEmail: string }> {
  console.error(`[caroline] [ratatosk-tools] resolveSession: as=${as}`);
  if (as === "owner") {
    const gate = requireSwOrPrompt(sendToFrontend);
    if (!gate.ok) throw new Error(gate.message);
    return { session: await getV2Session(), senderEmail: loggedInEmail()! };
  }
  if (!hasOwnRatatoskAccount(workspaceDir)) throw new Error("Caroline has no Ratatosk account yet -- use ensure_ratatosk_own_account first.");
  return { session: await getOwnV2Session(workspaceDir), senderEmail: ownRatatoskEmail(workspaceDir)! };
}

const asParam = z.enum(["owner", "caroline"]).describe(
  '"owner" acts as the user\'s own account (their messages, their contacts). "caroline" acts as your own ' +
    "separate Ratatosk account, if you have one (see ensure_ratatosk_own_account)."
);

export function createRatatoskTools(workspaceDir: string, sendToFrontend: SendLoginEvent): McpServerConfig {
  const identityStatus = tool(
    "ratatosk_identity_status",
    "Reports both Ratatosk identities: whether the user is logged into their own SquirrelWisdom account and " +
      "what its email is, and whether you (Caroline) have your own separate Ratatosk account and what ITS " +
      "email is. Call this before acting on Ratatosk if you're not sure which identity applies, or to avoid " +
      "confusing who sent/received a given message.",
    {},
    async () => {
      console.error("[caroline] [ratatosk-tools] ratatosk_identity_status called");
      const ownerEmail = isLoggedIn() ? loggedInEmail() : null;
      const carolineEmail = hasOwnRatatoskAccount(workspaceDir) ? ownRatatoskEmail(workspaceDir) : null;
      return {
        content: [{
          type: "text",
          text: JSON.stringify({
            owner: ownerEmail ? { loggedIn: true, email: ownerEmail } : { loggedIn: false },
            caroline: carolineEmail ? { registered: true, email: carolineEmail } : { registered: false },
          }),
        }],
      };
    },
  );

  const ensureOwnAccount = tool(
    "ensure_ratatosk_own_account",
    "Registers Caroline's own, separate Ratatosk account if one doesn't already exist (a real mailbox on " +
      "navlink.net plus a SquirrelWisdom account, both generated automatically -- no user input needed). " +
      "Idempotent: if you already have one, just reports it. Needed before using any ratatosk_* tool with " +
      'as:"caroline".',
    {},
    async () => {
      console.error("[caroline] [ratatosk-tools] ensure_ratatosk_own_account called");
      const result = await ensureOwnRatatoskAccount(workspaceDir);
      if (!result.ok) {
        console.error(`[caroline] [ratatosk-tools] ensure_ratatosk_own_account: failed: ${result.error}`);
        return { content: [{ type: "text", text: `Failed: ${result.error}` }], isError: true };
      }
      return { content: [{ type: "text", text: `Caroline's Ratatosk account: ${result.email}` }] };
    },
  );

  const listConvos = tool(
    "ratatosk_list_conversations",
    "Lists Ratatosk conversations (each a group, including 2-person DMs) for the given identity.",
    { as: asParam },
    async ({ as }) => {
      console.error(`[caroline] [ratatosk-tools] ratatosk_list_conversations called: as=${as}`);
      const { session, senderEmail } = await resolveSession(workspaceDir, as, sendToFrontend);
      const conversations = await listConversations(session, senderEmail);
      return { content: [{ type: "text", text: JSON.stringify(conversations) }] };
    },
  );

  const getMessages = tool(
    "ratatosk_get_messages",
    "Gets recent messages in a Ratatosk conversation (by groupId, from ratatosk_list_conversations) for the " +
      "given identity, oldest first.",
    { as: asParam, groupId: z.string(), days: z.number().int().min(1).max(14).optional().describe("How many days back to read (default 2).") },
    async ({ as, groupId, days }) => {
      console.error(`[caroline] [ratatosk-tools] ratatosk_get_messages called: as=${as} groupId=${groupId} days=${days ?? 2}`);
      const { session } = await resolveSession(workspaceDir, as, sendToFrontend);
      const messages = await getRecentMessages(session, groupId, days ?? 2);
      return { content: [{ type: "text", text: JSON.stringify(messages) }] };
    },
  );

  const sendMsg = tool(
    "ratatosk_send_message",
    "Sends a message in a Ratatosk conversation as the given identity. For as:\"owner\" this genuinely sends " +
      "as the user, indistinguishable from them typing it themselves -- you have standing authorization for " +
      "this specific channel (confirmed with the user), so no separate per-message confirmation is needed, " +
      "but the message content still has to be something you actually know to be true (never invent facts " +
      "in a message sent to a real person).",
    { as: asParam, groupId: z.string(), text: z.string() },
    async ({ as, groupId, text }) => {
      console.error(`[caroline] [ratatosk-tools] ratatosk_send_message called: as=${as} groupId=${groupId} text.length=${text.length}`);
      const { session, senderEmail } = await resolveSession(workspaceDir, as, sendToFrontend);
      await sendMessage(session, groupId, senderEmail, text);
      return { content: [{ type: "text", text: "Sent." }] };
    },
  );

  const startChat = tool(
    "ratatosk_start_chat_with",
    "Finds or creates a direct-message conversation with the given email address, as the given identity, " +
      "and returns its groupId (use that with ratatosk_get_messages/ratatosk_send_message).",
    { as: asParam, email: z.string() },
    async ({ as, email }) => {
      console.error(`[caroline] [ratatosk-tools] ratatosk_start_chat_with called: as=${as} email=${email}`);
      const { session, senderEmail } = await resolveSession(workspaceDir, as, sendToFrontend);
      const groupId = await findOrCreateDM(session, senderEmail, email);
      return { content: [{ type: "text", text: groupId }] };
    },
  );

  return createSdkMcpServer({
    name: "caroline-ratatosk",
    tools: [identityStatus, ensureOwnAccount, listConvos, getMessages, sendMsg, startChat],
  });
}

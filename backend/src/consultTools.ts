import { z } from "zod";
import { tool, createSdkMcpServer, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";
import { getV2Session } from "./login.js";
import { consultLargeModel } from "./voice.js";
import { requireSwOrPrompt } from "./swGate.js";

/**
 * A single tool letting Caroline ask a GPT-5-class model ("LARGE", see reforce's
 * AI.py model-category auto-selection) for wording advice on a non-technical
 * question -- per explicit instruction (2026-09-04): when discussing legal,
 * commercial, or social matters that Caroline herself judges to be high-complexity
 * AND high-importance, GPT-5-class models are measurably better at careful, nuanced
 * phrasing than Claude Code is (the same way Claude Code is the better one at actual
 * code) -- see policies.ts's consultLargeModelInstruction() for the exact steering
 * on when this is meant to be used. Gated on the user actually being logged into
 * their own SquirrelWisdom account ("при подключенной подписке SW") -- there's
 * nothing useful to consult against otherwise.
 */
export function createConsultTools(sendToFrontend: (event: { type: "open_login"; requestId: string; error?: string }) => void): McpServerConfig {
  const consultLarge = tool(
    "consult_large_model",
    "Asks a more capable, GPT-5-class model for advice on WORDING a response to a legal, commercial, or " +
      "social (non-technical) question -- per explicit instruction, use this when such a question is, by " +
      "your own judgment, both high-complexity AND high-importance (something the user will act on, sign, " +
      "send to someone else, or that carries real legal/financial/relationship consequences). This returns " +
      "ADVICE for you to weigh and incorporate into your own final answer -- it does not replace your own " +
      "response or speak directly to the user; you decide what to actually say. Only available when the " +
      "user is logged into their own SquirrelWisdom account (returns an error otherwise -- if that happens, " +
      "just proceed using your own judgment, same as before this tool existed).",
    {
      question: z.string().describe(
        "The question or draft wording to get advice on -- include enough context (what's being decided, " +
          "who's involved, what's at stake, the language it should be in) for genuinely useful advice."
      ),
    },
    async ({ question }) => {
      console.error(`[caroline] [tool:consult_large_model] question.length=${question.length}`);
      const gate = requireSwOrPrompt(sendToFrontend);
      if (!gate.ok) {
        return { content: [{ type: "text", text: gate.message }], isError: true };
      }
      try {
        const session = await getV2Session();
        const advice = await consultLargeModel(question, session);
        return { content: [{ type: "text", text: advice }] };
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        console.error(`[caroline] [tool:consult_large_model] failed: ${message}`);
        return { content: [{ type: "text", text: `Consultation failed: ${message}` }], isError: true };
      }
    },
  );

  return createSdkMcpServer({ name: "caroline-consult", tools: [consultLarge] });
}

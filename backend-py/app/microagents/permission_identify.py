"""turn.permission.identify -- step 1 of turn.permission.guard's two-step
gate (see reforce's apps/microagents/PermissionGuard.py own module
docstring for the full incident history). Pure fact-finding, no approval
judgment at all: from the recent dialogue alone, is there a pending
communication-on-the-user's-behalf or money action right now, and if so,
whose identity does it go out under?

Moved here from reforce's apps/microagents/PermissionIdentify.py (2026-10-17,
verbatim business logic, only the Microagent import changed) as the pilot
for running agent-specific CODE on Caroline's own side instead of inside
Camerlengo's server codebase -- see app/microagent_client.py's own module
docstring for why. Step 2 (turn.permission.guard) stays server-side for now
-- it is the agentic evolution engine's pilot role, a separate decision.

Per explicit instruction (2026-10-17): turn.permission.guard's single prompt
used to do identity classification AND approval-policy judgment in the same
pass -- exactly where incidents kept recurring (a crafted-persona identity
misread as the user's own identity, a reply-to-owner misread as third-party
communication, and the incident that prompted the original split: a
correction sent "от своего имени и от имени Пита Харриса" in one breath,
the assistant's own identity and a persona conflated together). Splitting
"what is true" from "what the policy says to do about it" into two
sequential calls means step 2 is handed a single, already-resolved identity
fact instead of re-deriving it under the same breath as the final verdict.
"""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

IDENTIFY = "turn.permission.identify"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_ACTION_TYPES = ("communication", "money", "none")
_SENDER_IDENTITIES = ("user_own", "assistant_own", "persona", "n/a")

_PROMPT = (
    "You are watching an AI assistant's conversation with its user, looking for exactly one thing: is "
    "there, right now, an action the assistant is about to do (or may already be doing) that falls into "
    "either of these two categories?\n\n"
    "(a) COMMUNICATION ON THE USER'S BEHALF: communication sent to a third party ON THE USER'S OWN "
    "BEHALF, through any channel (a message, an email, a post, a call, anything read by someone other "
    "than the user).\n\n"
    "(b) MONEY: anything that affects the user's own money or finances (a payment, a purchase, a "
    "transfer, anything that spends or commits their funds).\n\n"
    "This is a pure fact-finding pass -- you are NOT deciding whether approval exists or is needed, only "
    "what the action actually is and whose name/identity it goes out under, if there is one. A separate "
    "step decides the policy question afterward from what you report here.\n\n"
    "IMPORTANT EXCEPTION to (a): this only covers the assistant speaking or writing AS the user, to the "
    "user's own contacts, under the user's own name/account. It does NOT cover the assistant's OWN "
    "correspondence under its OWN separate, standing identity/account -- whether that's REPLYING to "
    "someone who reached out to IT, or the assistant ITSELF initiating or continuing a thread from that "
    "same standing identity (not a one-off alias invented for this one outreach, and not the user's own "
    "name) -- either way this is the assistant's own correspondence, not the user's, and belongs under (a) "
    "only if the dialogue shows it's actually going out AS the user. A tool/account whose own description "
    "or the dialogue itself establishes as the assistant's own (e.g. 'Caroline's own mailbox', an account "
    "the assistant manages under its own persona name) stays the assistant's own identity for EVERY send "
    "through it, not just replies -- confirmed live, 2026-10-18: an outreach email correctly summarized "
    "elsewhere as 'from Caroline Dickens's own account' was still misclassified as the user's own, purely "
    "because this exception used to only cover replying, never initiating/continuing from that same "
    "standing identity. The dialogue usually says which case applies in so many words -- trust that "
    "wording: a note that says the message arrived in (or goes out from) the assistant's 'own' "
    "account/identity, or explicitly invites a reply under its own name, means this is action_type "
    "\"none\" for that message; a note that explicitly calls an account or correspondence the user's own / "
    "the owner's own (not the assistant's) means (a) still applies. Watch for this exact confusion: an "
    "internal note is written TO the assistant, so 'you'/'your' INSIDE such a note means the assistant "
    "itself (e.g. 'a message arrived in your own account' = the assistant's own account, not the "
    "user's).\n\n"
    "ANOTHER IMPORTANT EXCEPTION to (a): replying to, or reporting findings/status/results DIRECTLY TO, "
    "the user/owner THEMSELVES is NEVER 'a third party' -- this is action_type \"none\", no matter how "
    "the dialogue refers to them: by name ('tell Kostia', 'report to Kostya'), by role ('the owner', 'the "
    "user', 'your boss'), or not explicitly at all. A name is not evidence of a second person -- it is "
    "very often simply how the dialogue refers to the one user this assistant serves. This applies with "
    "full force even in a dialogue built entirely from the assistant's own proactive/scheduled notes with "
    "no real back-and-forth in it at all.\n\n"
    "If (a) or (b) applies, report:\n"
    "- action_summary: one short, plain sentence describing the action (what is being sent/spent, to/for "
    "whom, roughly what it says or covers).\n"
    "- recipient: who or what it targets (a name, an account, a payee) -- empty string if there is none to "
    "name.\n"
    "- sender_identity: whose name/account the action goes out under. Exactly one of:\n"
    "  \"user_own\" -- the user's actual personal name or account: their own name, their own email "
    "address, their own messenger account. NEVER a crafted sender persona, alias, or business/brand name "
    "made up for outreach (a fictional sales rep's name, a company representative identity, a pen name) -- "
    "even when the assistant composed it for the user, the outreach serves the user's own goals, and the "
    "user themselves asked for it to be sent. That case is \"persona\", not \"user_own\".\n"
    "  \"assistant_own\" -- the assistant's own separate identity/account (see the exception above).\n"
    "  \"persona\" -- a crafted sender persona, alias, or business/brand identity, never the user's real "
    "name or the assistant's own bot identity.\n"
    "  \"n/a\" -- action_type is \"money\", or there is genuinely no sender identity to classify.\n"
    "- wording_shown_and_accepted: true ONLY when sender_identity is \"user_own\" AND the dialogue shows "
    "the SPECIFIC drafted wording (not just the topic/intent) was shown to the user and the user accepted "
    "it outright ('send it', 'looks good') or did not object after seeing it, OR the user dictated the "
    "message's actual words themselves (quoted it, or gave text so precise and complete there was nothing "
    "left for the assistant to compose). Otherwise false, including whenever sender_identity is not "
    "\"user_own\" (this fact only matters for that one case).\n"
    "- explicitly_instructed_by_user: true when the user's OWN most recent real message is itself a DIRECT "
    "COMMAND to perform this exact action -- an imperative telling the assistant to send/write/pay/do this "
    "specific thing (e.g. 'Write to Leah and ask about X', 'Send Oksana the schedule', 'Pay the invoice'), "
    "not merely the user discussing, mentioning, or describing a situation the assistant then decided on its "
    "own to act on. This is a DIFFERENT fact from wording_shown_and_accepted: the user does not need to have "
    "seen or approved the exact drafted TEXT for this to be true -- they only need to have clearly, directly "
    "told the assistant to carry out this action, in so many words, themselves. True regardless of "
    "sender_identity. False whenever the action was the assistant's OWN idea/initiative, or only loosely "
    "implied, or the user's message was ambiguous about whether to actually act.\n\n"
    "If NEITHER (a) nor (b) applies to anything currently pending, set has_pending_action=false, "
    "action_type=\"none\", and leave action_summary/recipient empty and sender_identity=\"n/a\"/"
    "wording_shown_and_accepted=false/explicitly_instructed_by_user=false.\n\n"
    "Decide using ONLY what is actually said in the dialogue below -- never guess or assume. When the "
    "dialogue shows SEVERAL distinct pending actions under different identities (e.g. one correction sent "
    "under the assistant's own name and another under a persona in the same breath), report the MOST "
    "RECENT/most pressing one -- the one a reader would need a policy decision about right now -- as a "
    "single, specific action; never merge two different actions or two different identities into one "
    "vague summary.\n\n"
    "Per explicit instruction (2026-10-18, the action-boundary tracking system): when action_type is not "
    "\"none\", also decide whether the pending action you just identified is the SAME bounded operation as "
    "one already listed under OPEN ACTIONS below (e.g. \"send the Four Faith renewal email to Jessica Zhou\" "
    "stays the same operation across several turns even if its wording drifts -- drafting it, then revising "
    "it, then actually sending it, are all the SAME operation, not a new one each time) -- if so, set "
    "matched_action_id to that action's own id, copied EXACTLY. If it is a genuinely different operation "
    "(or OPEN ACTIONS is empty), set matched_action_id to null. Never invent an id that isn't listed.\n\n"
    f"TASK (the user's request):\n---\n{{task}}\n---\n\n"
    f"RECENT DIALOGUE (oldest first, last line is the newest):\n{{recent_dialogue}}\n\n"
    f"OPEN ACTIONS (operations already tracked, each with its own id -- empty means none are open):\n{{open_actions_block}}\n\n"
    "Reply now."
)


def _open_actions_block(open_actions: Any) -> str:
    if not open_actions:
        return "(none)"
    return "\n".join(f"- id: {a['id']} -- summary: {a['summary']}" for a in open_actions)


def prompt_identify(input: Any, context: Any) -> str:
    return _PROMPT.format(
        task=input["task"] or "(not given)", recent_dialogue=input["recent_dialogue"] or "(empty)",
        open_actions_block=_open_actions_block(input.get("open_actions")),
    )


def verify_identify(input: Any, output: Any) -> Optional[str]:
    action_type = output.get("action_type")
    if action_type not in _ACTION_TYPES:
        return f'"action_type" must be one of {_ACTION_TYPES}'
    sender_identity = output.get("sender_identity")
    if sender_identity not in _SENDER_IDENTITIES:
        return f'"sender_identity" must be one of {_SENDER_IDENTITIES}'
    if not isinstance(output.get("has_pending_action"), bool):
        return '"has_pending_action" must be a boolean'
    if not isinstance(output.get("wording_shown_and_accepted"), bool):
        return '"wording_shown_and_accepted" must be a boolean'
    if not isinstance(output.get("explicitly_instructed_by_user"), bool):
        return '"explicitly_instructed_by_user" must be a boolean'
    if action_type != "none" and not (output.get("action_summary") or "").strip():
        return '"action_summary" must be a non-empty string when action_type is not "none"'
    matched_action_id = output.get("matched_action_id")
    if matched_action_id is not None:
        known_ids = {a["id"] for a in (input.get("open_actions") or [])}
        if matched_action_id not in known_ids:
            return f'"matched_action_id" must be null or one of the given open_actions ids, got {matched_action_id!r}'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            IDENTIFY, "1",
            "Pure fact-finding step for turn.permission.guard: from the recent dialogue, is there a "
            "pending communication-on-the-user's-behalf or money action, what is it, whose identity "
            "does it go out under, and (per the action-boundary system) which already-open action -- "
            "if any -- it is the same operation as. No approval judgment.",
            input_schema=_object(
                {"task": _STRING, "recent_dialogue": _STRING, "open_actions": {"type": "array", "items": {"type": "object"}}},
                required=["task", "recent_dialogue"],
            ),
            output_schema=_object(
                {
                    "has_pending_action": {"type": "boolean"},
                    "action_type": {"type": "string", "enum": list(_ACTION_TYPES)},
                    "action_summary": _STRING,
                    "recipient": _STRING,
                    "sender_identity": {"type": "string", "enum": list(_SENDER_IDENTITIES)},
                    "wording_shown_and_accepted": {"type": "boolean"},
                    "explicitly_instructed_by_user": {"type": "boolean"},
                    "matched_action_id": {"type": ["string", "null"]},
                },
                required=[
                    "has_pending_action", "action_type", "action_summary", "recipient", "sender_identity",
                    "wording_shown_and_accepted", "explicitly_instructed_by_user", "matched_action_id",
                ],
            ),
            build_prompt=prompt_identify, verify=verify_identify,
        ),
    ]

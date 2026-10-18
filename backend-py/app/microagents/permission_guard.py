"""turn.permission.guard -- a microagent that decides, from the recent
dialogue alone, whether Caroline (the Camerlengo-backed AI assistant) is
currently sitting on an action that needs the user's explicit permission
and does not have it yet.

Per explicit instruction (2026-10-12), narrowing an earlier, broader
version of this agent: approval is needed ONLY for two kinds of action --
(a) communication sent on the user's behalf to a third party, and (b)
anything that affects the user's money/finances. An unanswered yes/no
question about anything else (checking something, restarting a process,
reading more files, routine exploration) never needs approval here,
answered or not -- this agent used to flag ANY unanswered permission
question on its own, which was wider than the user actually wanted
gated. The two categories are still, each on its own, a reason to need
approval the same way: communication on the user's behalf and money
always need a clear, specific prior approval already present in the
dialogue; a general "go ahead, do what's needed" is not enough for a
particular send or a particular spend.

Per explicit instruction (2026-10-12): once a clear approval for a given
send/spend is already in the dialogue, it covers that same action going
forward -- this agent must not flag needs_approval again for an action
the user already approved just because it is asked again later (the
caller re-runs this after every outbound message). Approval is sticky
per action, not single-use; it only fails to cover a genuinely NEW/
different send or spend that hasn't itself been approved yet.

Bug fix (2026-10-12), confirmed live (a real incident: "Поблагодари Влада,
напиши, что его находки по аудиту очень ценны и проблемы уже
исправляются" -- an explicit, specific, imperative order -- was blocked
as unapproved 5 times in a row). Root cause: the prompt described
"approval" only in terms of answering a yes/no question the assistant
itself asks, never saying in so many words that a direct order FROM the
user, naming the action, already IS the approval with no question-answer
exchange needed at all -- the far more common real-world case. Fixed by
adding that as its own explicit rule, distinct from the yes/no-question
rule (which still applies, unchanged, when the ASSISTANT is the one
proposing the action and asking whether to proceed).

Per explicit instruction, classification of "is this communication on the
user's behalf" / "does this affect finances" is NOT a static tag on a tool
or a name-based heuristic -- it is this agent's own contextual judgment,
from the dialogue, the same way AddressBook.py's matching is entirely the
model's call rather than code-side string comparison (decision of
2026-10-10, same codebase).

Per explicit instruction (2026-10-11, "в отсутствие явного запрета может и
будет отвечать сразу же"): replying under the assistant's OWN separate
identity on a messenger (e.g. Telegram/Ratatosk's own distinct "self"/
"caroline" account, or a Discord bot identity, which never acts as the
user at all) is NOT "communication on the user's behalf" and must never be
held up by this agent absent an actual explicit prohibition -- only
speaking/writing AS the user, under their own name or account (Telegram's
"owner" account, WhatsApp, Slack, Ratatosk's "owner" session), needs the
approval rule 2 describes. Caroline's own incoming-message notices already
say in plain words which case applies (see telegram_channel.py's own
notification text for the clearest example) -- the prompt below leans on
that existing wording rather than inventing new structured metadata for it.

Bug fix (2026-10-13), confirmed live, a real incident: Caroline's new
headless background-task session (no chat window, carries reminder-driven
work like periodic mailbox/messenger sweeps -- see chat_session.py's
BACKGROUND_TAB_ID) got stuck in a 9-times-and-counting loop, each time
blocked with a reason like "Sending a status line to Kostia would be
third-party communication about the user; explicit permission is
required." Kostia IS the user/owner Caroline serves -- not a third party
at all. Root cause: that session's own dialogue is built entirely from
synthetic proactive injects (never a real back-and-forth with the user
typing), so nothing in it structurally LOOKS like "a conversation with the
user" the way a real chat does -- the user is only ever referred to by
NAME ("report anything new to Kostia"), and with no other signal, this
agent inferred a second person being talked about/to on the user's behalf,
rather than recognizing the name as the user themselves. Fixed by making
the carve-out explicit and unconditional: replying to, or reporting
findings/status/results DIRECTLY TO, the user/owner THEMSELVES is NEVER
"a third party" under rule (a), no matter how the dialogue refers to them
-- by name, by role ("the owner", "the user", "Kostia", "your boss"), or
not at all. A name is not evidence of a second person; do not require the
literal words "the user" to be present before recognizing this. This
applies with equal force however thin or purely-proactive the dialogue is
-- a session with no real user turn in it at all is still reporting to
its own user, not to a stranger, and must never be held up on that
account.

This agent does not look at any specific tool call -- only at the
dialogue. The caller (Caroline's own chat_session.py) re-runs it after
every outbound message and every new real user message, and uses its
`needs_approval` flag to gate ALL tool execution for that turn (see
app/operations.py's dispatch()) until it is cleared.

Bug fix (2026-10-16), confirmed live, a real incident: Caroline sent an
email under the OWNER'S OWN identity (kostia.khait@gmail.com, "Константин
Хайт") to a sensitive third party -- the recipient and rough content had
been discussed, but the user had never seen or approved the actual drafted
sentences before they went out. Per explicit instruction ("Отправка текста
от моего имени должна быть согласована по содержанию. Кэролайн регулярно
лажает с ним"): approving the ACTION (send something to X, covering Y) is
no longer enough by itself for a user's-own-identity send -- the EXACT
WORDING section below now requires the specific drafted text to have been
shown to the user and accepted before it may go out under their name,
separate from and in addition to whether the send itself was ordered. This
deliberately narrows the earlier "a direct specific instruction is itself
the approval" rule for THIS one case (own-identity sends): it still covers
whether to send at all, it no longer also covers the wording the assistant
composed to do it. Scoped to identity (a), not (b)/money, and not to the
assistant's own separate identity -- see the existing exceptions for both,
unchanged.

Bug fix (2026-10-16), confirmed live, a real incident, same day as the one
just above but the opposite direction: a cold-outreach campaign sending
under a crafted sales persona (not the owner's own name at all -- a made-up
sender identity used to reach VPN-provider companies) got re-blocked before
EVERY SINGLE recipient's email for the exact-wording reason the prior fix
just added, even though the user had already approved the whole list once.
Root cause: the EXACT WORDING rule above talks about "the user's OWN
identity" vs. "the assistant's own separate identity" and nothing else --
with no other case named, the model classified a persona that plainly
wasn't the assistant's own bot identity as the user's own identity by
elimination, so the SAME per-message-wording requirement meant for a real
kostia.khait@gmail.com send got wrongly applied to every one of ~12 VPN
company emails under a fictional sender name. Per explicit instruction
("эти письма отсылаются не от моего имени... Любая не-личная персона"):
EXACT WORDING's own identity scope is now explicit about a third case
(a crafted persona/alias/brand is NEVER "the user's own identity", no
matter how closely it serves the user's own goals). A persona-based send
still needs rule (a)'s own action-level approval (which the user already
gave for the whole list, and STICKY APPROVAL already covers), it just never
needs separate wording sign-off per recipient the way an own-identity send
does.

Split into two steps (2026-10-17), per explicit user instruction, after yet
another identity-conflation incident (a correction sent "от своего имени и
от имени Пита Харриса" in one breath -- the assistant's own identity and a
persona mixed together in the SAME turn, the same class of bug as the two
incidents directly above). Identity/action classification (the former
"WHICH IDENTITY COUNTS AS THE USER'S OWN" and both "IMPORTANT EXCEPTION"
paragraphs) moved out entirely into a new sibling agent,
turn.permission.identify (PermissionIdentify.py), which runs FIRST and
reports its findings here as ESTABLISHED FACTS (see _established_facts_
section below) -- this agent no longer re-derives whose identity a pending
action is under, it only applies approval policy to what step 1 already
established. The orchestration (two sequential calls, short-circuiting
straight to needs_approval=false when step 1 finds no pending action at
all) lives in Api2ReplyCommands.cmdV2ReplyPermissionGuard, not here --
this module still owns only the single GUARD agent and its own prompt/
verify/schema."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

GUARD = "turn.permission.guard"

# Reverted (2026-10-16), per explicit instruction, back to the standard
# ladder (M.LADDER: nano -> nano-with-rejection-reason -> mini) -- this
# agent used to skip straight to MODEL_MINI (2026-10-15 fix below, kept for
# history), after a real incident where nano misjudged an unambiguous,
# explicit, dictated instruction. That fix traded cost for safety; cost
# analysis (2026-10-16, real usage log) showed turn.permission.guard is
# BOTH the single most-called agent of this whole file (far more than
# reply.review/translate/actionSummary/replyExpected combined) AND, because
# it alone skipped nano, responsible for ~91% of the reply: domain's entire
# OpenRouter spend in one sampled window ($0.61 of $0.67) -- every other
# agent here resolved on nano alone, every single sampled call. Per
# explicit instruction, back on the standard ladder: nano gets its normal
# two tries (including the rejection-reason retry) before mini, same as
# every other agent in this file.
#
# Bug fix (2026-10-15), confirmed live, kept for context: the default
# ladder's rung 1 (MODEL_NANO, openai/gpt-5-nano, reasoning "low") missed an
# unambiguous, explicit, dictated instruction to send a specific message to
# a specific named recipient -- not a malformed-output failure (which is
# the only thing that makes the default ladder escalate past nano at all),
# a genuine misjudgment of context already present in the dialogue.

# Per explicit instruction (2026-10-15): the client used to reset its own
# escalation counter (how many consecutive blocks before it stops asking the
# model to request permission and asks the user directly itself) on EVERY new
# real user message -- including a message that was only a CLARIFICATION of
# the same still-pending action, which kept earning the model a fresh "ask
# nicely yourself" grace cycle for something already explained twice. Per
# explicit instruction ("почему мы делаем строковое сравнение вместо LLM-
# сравнения... бели маленькую модель и сравнивай"): sameness is judged by
# THIS agent, semantically, using the whole dialogue it already reads --
# never a hardcoded string/slug comparison on the client, which a confirmed
# live wording drift (the SAME action described two different ways across
# two consecutive real calls) would have broken anyway. See
# same_action_as_before/_previous_block_clause below.

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


# Evolution-engine genome (2026-10-17): the pure business-rule prose below
# is the ONLY thing turn.permission.guard's Mutator may rewrite -- split out
# of prompt_guard as a plain opaque string specifically so a clone's
# build_prompt can be `GUARD_RULES_TEXT_VARIANT + _data_section(input)`
# with no risk of touching the per-call data wiring (_previous_block_clause/
# TASK/RECENT DIALOGUE) or, separately and never touched by any of this,
# the schema/verify_guard contract below. This split is purely mechanical:
# GUARD_RULES_TEXT is exactly the string prompt_guard already built, up to
# (not including) the per-call data section, byte for byte.
GUARD_RULES_TEXT = (
    "You are watching an AI assistant's conversation with its user, looking for exactly one thing: "
    "is there, right now, an action the assistant is about to do (or may already be doing) that needed "
    "the user's explicit permission and does not have it?\n\n"
    "Approval is required ONLY for these two kinds of action -- nothing else, ever, under this agent, no "
    "matter how the dialogue reads:\n\n"
    "(a) COMMUNICATION ON THE USER'S BEHALF: communication sent to a third party ON THE USER'S OWN "
    "BEHALF, through any channel (a message, an email, a post, a call, anything read by someone other "
    "than the user).\n\n"
    "(b) MONEY: anything that affects the user's own money or finances (a payment, a purchase, a "
    "transfer, anything that spends or commits their funds).\n\n"
    "For either kind, that specific action always needs a clear, explicit, specific approval already "
    "present in the dialogue. A general 'go ahead' or 'do what's needed for this task' does NOT count as "
    "specific approval for a particular send or a particular spend; it needs an answer that is clearly "
    "about that exact action. An unanswered yes/no permission question ('can I...', 'should I...', 'may "
    "I...') about a send or a spend counts the same way: no reply, a reply about something else, or an "
    "ambiguous/non-committal reply are ALL treated as not approved.\n\n"
    "A DIRECT, SPECIFIC INSTRUCTION FROM THE USER IS ITSELF THE APPROVAL -- this is the common case, not "
    "an edge case, and it needs no separate yes/no exchange on top of it. Most of the time nobody asks "
    "'can I send this?' at all: the user simply orders the send/spend outright ('thank X for their audit "
    "findings and tell them the issues are already being fixed', 'send Y the attached file', 'pay the "
    "$50 invoice'). When the user's own words already name who the message goes to and/or what it should "
    "say, or which payment to make, that instruction IS the clear, explicit, specific approval this agent "
    "requires, the moment it's given -- never flag needs_approval for it, and never wait for the user to "
    "additionally confirm 'yes' to a question that was never asked, because the assistant correctly "
    "going on to do exactly what it was just told is not a new action still awaiting approval. The "
    "yes/no-question rule above is for a DIFFERENT situation: the assistant itself proposing a send/spend "
    "and asking the user whether to proceed -- there, an actual answer is still required. Do not collapse "
    "the two: an order from the user needs no question asked of them at all; a question from the "
    "assistant needs an actual answer.\n\n"
    "Nothing else ever needs approval here. An unanswered yes/no question about anything OTHER than a "
    "send or a spend -- checking something, restarting a process, reading more files, continuing "
    "ordinary exploration or work -- is NOT a reason to flag needs_approval, answered or not; only (a) "
    "and (b) are in scope.\n\n"
    "STICKY APPROVAL: once the dialogue already shows a clear approval for a specific send/spend, that "
    "approval covers that same action from then on -- do not flag needs_approval again for an action the "
    "user already clearly approved just because this check runs again later (it runs after every "
    "outbound message). Only flag a genuinely NEW/different send or spend that hasn't itself been "
    "approved yet; do not re-litigate one that already was.\n\n"
    "MULTI-STEP SEND: when the user's own order to send/spend requires the assistant to do a bit of work "
    "first -- find the recipient's address from notes or prior messages, look up an account number, "
    "finish writing the draft, ask the user a factual question like which address to use -- that "
    "in-between work does NOT create a new approval gap for the ACTION itself (whether to send/spend "
    "something to this recipient at all). Do not flag needs_approval partway through just because the "
    "assistant is still gathering information, still drafting, or has asked (or already gotten an answer "
    "to) a follow-up logistics question like which address to use.\n\n"
    "ESTABLISHED FACTS below (from a separate classification step that already ran -- trust them, do not "
    "re-derive whose identity the action is under or what the action is; they are your ground truth for "
    "that, the dialogue is still yours to read for everything about approval itself) tell you, for the "
    "current pending action if there is one: its type, a one-sentence summary, the recipient, whose "
    "identity it goes out under (sender_identity: \"user_own\" the user's real personal name/account, "
    "\"assistant_own\" the assistant's own separate identity -- never needs approval under (a) at all, the "
    "same way a human secretary doesn't need sign-off to answer their own phone -- \"persona\" a crafted "
    "sender alias/brand never mistakable for the user's own real words, or \"n/a\"), and whether the "
    "SPECIFIC drafted wording was already shown to and accepted by the user (wording_shown_and_accepted).\n"
    "If sender_identity is \"assistant_own\", this is not rule (a) communication at all -- never flag it.\n\n"
    "EXACT WORDING, for sender_identity \"user_own\" specifically (never for \"assistant_own\" or "
    "\"persona\" -- a persona send still needs rule (a)'s own clear order/approval of the action, covering "
    "every recipient in a list once given for the list as a whole per STICKY APPROVAL, but never this "
    "extra per-message wording sign-off, because nobody reading it could ever mistake it for the user's "
    "own real words in the first place): approval of the ACTION (that a message should go to this "
    "recipient, saying roughly this) is NOT, by itself, approval of the exact sentences the assistant "
    "drafted to say it. The user describing what the message should cover or achieve -- the facts to "
    "include, the tone, the ask -- is an order to act, not a transcript to send verbatim; the assistant "
    "still composed the actual wording itself. Flag needs_approval if wording_shown_and_accepted is false "
    "for a user_own send. The one exception, already folded into wording_shown_and_accepted: the user "
    "dictated the message's actual words themselves (quoted it, or gave text so precise and complete "
    "there was nothing left for the assistant to compose) -- there, sending it as given needs no separate "
    "re-approval. This is EXTRA weight on top of rule (a) above, not a replacement for it -- both must "
    "hold: the action itself must have a clear order or approval, AND (for sender_identity \"user_own\") "
    "wording_shown_and_accepted must be true. Only flag needs_approval if the assistant is about to send "
    "something genuinely DIFFERENT from what was ordered (a different recipient, a materially different "
    "message) with no approval for that difference, OR -- per EXACT WORDING above -- wording_shown_and_"
    "accepted is false for a user_own send. The TASK field below is only the single most recent user "
    "message, which is very often a short follow-up ('yes, send it', 'use the address from our chat', "
    "'the text is fine') rather than a restatement of the whole request -- always read the FULL dialogue "
    "for the actual standing instruction and whatever approval is already in it; a terse or "
    "logistics-only TASK field is never itself a sign that approval is missing.\n\n"
    "Decide using ONLY what is actually said in the dialogue below (plus the ESTABLISHED FACTS given to "
    "you) -- never guess or assume consent. If "
    "neither (a) nor (b) applies, or the needed approval is clearly already there, say so. If you do find a "
    "live gap, describe in one short, plain sentence (for the assistant itself to read) exactly what "
    "still needs the user's permission.\n\n"
)


def _established_facts_section(input: Any) -> str:
    """The step-1 (turn.permission.identify) output, handed to this agent as
    given ground truth -- see GUARD_RULES_TEXT's own ESTABLISHED FACTS
    paragraph. action_type "none" (or no facts given at all, for a direct
    call bypassing the two-step orchestration, e.g. in a test) renders as
    "no pending action reported" rather than guessing."""
    action_type = input.get("action_type") or "none"
    if action_type == "none":
        return "ESTABLISHED FACTS: no pending communication or money action reported by the classification step.\n\n"
    return (
        "ESTABLISHED FACTS (from the separate classification step):\n"
        f"- action_type: {action_type}\n"
        f"- action_summary: {input.get('action_summary') or '(not given)'}\n"
        f"- recipient: {input.get('recipient') or '(not given)'}\n"
        f"- sender_identity: {input.get('sender_identity') or 'n/a'}\n"
        f"- wording_shown_and_accepted: {bool(input.get('wording_shown_and_accepted'))}\n\n"
    )


def _data_section(input: Any) -> str:
    """The per-call wiring (never part of the genome, never touched by the
    Mutator): established facts, previous-block sameness clause, TASK,
    RECENT DIALOGUE."""
    return (
        f"{_established_facts_section(input)}"
        f"{_previous_block_clause(input)}"
        f"TASK: {input['task'] or '(not given)'}\n\n"
        f"RECENT DIALOGUE (oldest first, last line is the newest):\n{input['recent_dialogue'] or '(empty)'}"
    )


def prompt_guard(input: Any, context: Any) -> str:
    return GUARD_RULES_TEXT + _data_section(input)


def _previous_block_clause(input: Any) -> str:
    previous = (input.get("previous_block_reason") or "").strip()
    if not previous:
        return ""
    return (
        "ALSO DECIDE SAMENESS, if you flag needs_approval=true: the assistant was PREVIOUSLY stopped, waiting "
        "on this exact reason:\n"
        f'"{previous}"\n'
        "Is the action you are flagging right now the SAME real-world action as that one (same recipient/"
        "payee, same channel, same underlying send or spend -- even if your own wording of the reason comes "
        "out differently this time), or a genuinely DIFFERENT action? Judge this purely on substance, never on "
        "whether your two sentences happen to match word for word -- your own phrasing is not guaranteed to "
        "repeat itself. Set same_action_as_before accordingly. If needs_approval is false, or no previous "
        "reason was given above, same_action_as_before is simply false -- there is nothing for it to compare "
        "against.\n\n"
    )


def verify_guard(input: Any, output: Any) -> Optional[str]:
    if output.get("needs_approval") and not (output.get("reason") or "").strip():
        return '"reason" must be a non-empty string when "needs_approval" is true'
    if not isinstance(output.get("same_action_as_before"), bool):
        return '"same_action_as_before" must be a boolean'
    return None


def llm_agents() -> List[M.Agent]:
    return [
        M.Agent(
            GUARD, "1",
            "Decides, from the recent dialogue plus turn.permission.identify's already-established facts, "
            "whether an un-approved communication-on-the-user's-behalf or financial action is currently "
            "outstanding; an already-approved action is never flagged again.",
            input_schema=_object(
                {
                    "task": _STRING, "recent_dialogue": _STRING, "previous_block_reason": _STRING,
                    "action_type": _STRING, "action_summary": _STRING, "recipient": _STRING,
                    "sender_identity": _STRING, "wording_shown_and_accepted": {"type": "boolean"},
                },
                required=["task", "recent_dialogue"],
            ),
            output_schema=_object(
                {"needs_approval": {"type": "boolean"}, "reason": _STRING, "same_action_as_before": {"type": "boolean"}},
                required=["needs_approval", "reason", "same_action_as_before"],
            ),
            build_prompt=prompt_guard, verify=verify_guard,
        ),
    ]

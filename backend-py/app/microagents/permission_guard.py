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
verify/schema.

Narrowed (2026-10-20), per explicit instruction: deciding whether a send or
a payment is allowed moved out of this agent entirely, into plain code run
right before every send (app/permission_policy.py: the identify step
establishes the facts, a table decides -- "ЛЛМ проверяет тип операции и от
чьего имени, а алгоритм (!) разрешает или блокирует"). This agent keeps its
own, different job only: "чтобы модель не отвечала сама себе" -- catching
the assistant going ahead on a question it asked the user and the user
never answered. It blocked a send from Caroline's own Telegram account over
an "unapproved caption", and again right after the user's explicit
"Разрешаю"; neither is a question left unanswered."""

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
    "You are watching an AI assistant's conversation with its user, looking for exactly ONE thing: is the "
    "assistant answering its own question?\n\n"
    "That is: the assistant ITSELF asked the user something that needs the USER's decision -- whether to "
    "do something, which of several options to take, a yes/no, a confirmation of a draft it showed -- and "
    "the user has not actually answered it, yet the assistant is going ahead (or is about to) as if they "
    "had: taking silence, its own guess, or a reply about something else as the answer.\n\n"
    "Flag needs_approval ONLY in that case. Never flag anything else; in particular:\n"
    "- Whose name or account something goes out under, whether a message or a payment is allowed at all, "
    "whether its exact wording was approved -- none of that is your concern. A separate rule checks every "
    "send and every payment right before it runs; do not repeat its job, and never flag because of it.\n"
    "- A direct order from the user ('send X to Y', 'write to Leah and ask about Z', 'pay this invoice') is "
    "not a question the assistant asked. Carrying it out is never answering itself.\n"
    "- Any actual answer from the user answers the question, however short: 'yes', 'да', 'Разрешаю', "
    "'go ahead', 'send it', 'ok', picking one of the options, giving the information asked for. Once "
    "answered, a question stays answered: this check runs again after every message -- never flag the "
    "same, already answered question a second time.\n"
    "- A question the user answered with a different instruction instead: the new instruction stands, and "
    "acting on it is not answering itself.\n"
    "- A question the assistant itself withdrew, and remarks that hold nothing up ('let me know if you "
    "want...', 'I can also...'), are not open questions.\n\n"
    "The ESTABLISHED FACTS below only tell you which action is pending; they are context, not something "
    "to judge. Decide using ONLY what is actually said in the dialogue -- never guess at an answer the "
    "user did not give. If you flag, say in one short, plain sentence (for the assistant itself to read) "
    "which question still waits for the user's answer.\n\n"
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
            "Decides, from the recent dialogue, whether the assistant is going ahead on a question it asked "
            "the user and the user never answered -- answering its own question.",
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

"""reply.review -- a microagent that checks and, when needed, rewrites a
single piece of outbound text (a progress-narration remark or a real final
reply) from Caroline, the Camerlengo-backed AI assistant. Per explicit
instruction: every such message must be checked for (1) relevance to the
task it is actually part of, (2) absence of internal implementation
details -- tool names, specific internal agent names, or mechanics such as
"dehydration"/compaction/retries/restarts -- and (3) sounding like a human's
reply, not an AI's.

The three criteria below are a re-authoring (not an import -- this module
lives in a different process/repository than Caroline's own
backend-py/app/policies.py, whose no_internal_mechanics_to_user_instruction()
is the system-prompt-only mechanism this microagent exists to back up with
an independent check) of that same list, confirmed live (2026-10-11) against
186 real progress-narration remarks pulled from a live Caroline installation's
log: no outright leak was found in that sample, but words like "decoding" or
"memory backup" are indistinguishable from an internal-mechanics leak without
knowing the task -- the user was literally asking for a video encode/decode
feature and a phone memory backup, so those words were legitimate task
vocabulary, not Caroline's own plumbing. That is why `task` is a required
input alongside `text`: the model needs the task to tell apart a feature noun
from an internal-mechanism name.
"""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

REVIEW = "reply.review"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


# Evolution-engine genome (2026-10-17): see PermissionGuard.py's own
# GUARD_RULES_TEXT comment for the split's rationale.
REVIEW_RULES_TEXT = (
        "You are reviewing one short piece of text an AI assistant is about to show its user, in the "
        "middle of carrying out the user's own task. Check it against three rules, and rewrite it only if "
        "it fails one of them -- if it already passes all three, return it completely unchanged, word for "
        "word; do not rephrase a message that is already fine just because you can.\n\n"
        "1. RELEVANT TO THE TASK: the text must actually be about the task below, not a non sequitur or a "
        "generic filler line that could apply to any task.\n\n"
        "2. NO INTERNAL MACHINERY: never expose the assistant's own internal implementation. That means no "
        "file paths, no session/turn/context internals, no mention of dehydration, compaction, archives, "
        "dumps, resets, restarts, retries, internal hangs or watchdog recoveries, stub notes, continuity "
        "files, the assistant's own workspace layout, the literal names of its tools, agents or plugins, or "
        "any 'standing instruction/lesson' it is following. This is about the ASSISTANT's own plumbing, not "
        "about the task's own subject matter -- if the task itself is about encoding/decoding video, or about "
        "backing up the user's own phone memory, those words are the task's own legitimate vocabulary and must "
        "stay; only strip or rephrase around words that describe the ASSISTANT's internal workings, not the "
        "user's own subject.\n"
        "This rule ALSO covers raw structured data quoted verbatim -- a JSON object/dict, a key-value blob, "
        "a tool call's literal return value (e.g. {\"ok\": true, \"url\": \"about:blank\", \"cdpPort\": 9900}), "
        "a stack trace, or any other machine-shaped fragment pasted into the reply instead of described in "
        "plain words. This is unambiguous regardless of the task -- no task ever makes a literal field name "
        "like 'cdpPort' or 'ok' appropriate to show a person -- so check for and strip it even when TASK "
        "below is '(not given)'; rewrite what it MEANS (what happened, in plain language) and drop the raw "
        "structure entirely, never leave it sitting alongside the plain-language sentence either.\n"
        "This rule ALSO covers a credential or secret value recited in plain prose (a password, API key, "
        "token, PIN or similar), not just JSON-shaped data -- mask the literal value itself. But go further "
        "than just masking it: a message whose only real content WAS that credential (e.g. 'Credentials for "
        "X: [password], [server], [port]') has NOTHING left to say once the secret is gone, and masking the "
        "value alone leaves a pointless, still off-task stub -- confirmed live, 2026-10-14: the assistant was "
        "asked to check a mailbox for journal confirmations, fetched its own login credentials as an internal "
        "step along the way, and recited them back as if that answered the question; masking the password "
        "alone ('Credentials for khait@multi-portal.org: [REDACTED], [REDACTED], [REDACTED].') still reached "
        "the user as a message, right before the real answer arrived separately a few seconds later. Reciting "
        "one's own login details is essentially never what TASK is actually asking for -- when TEXT is this "
        "shape, treat it as failing rule 1 too and collapse the WHOLE message to [[NO_UPDATE]], not a masked "
        "remnant of it, UNLESS TEXT also contains separate, genuine substance that answers TASK.\n"
        "This rule ALSO covers the assistant's own BACKGROUND-TASK bookkeeping: a self-scheduled reminder "
        "firing, a periodic maintenance sweep, a memory/notes backup, a 'vault note' or similar internal "
        "continuity record and its timestamp, or any other internal task-scheduling detail -- these are the "
        "assistant's own plumbing for running itself, not something the user asked about, even when the task "
        "the assistant is actually doing right now IS one of these background chores (confirmed live, "
        "2026-10-13: 'Now updating the vault note to add the Lev/Switzerland item, then running the mailbox "
        "sweep' and 'Vault note content is unchanged and current, just needs the timestamp bump' both leaked "
        "through unchanged). When a block fails this way, rewrite it to the user-relevant SUBSTANCE in plain "
        "words -- what was actually found or decided, if anything worth telling the user -- or, if there is "
        "genuinely nothing worth telling the user, return EXACTLY the sentinel [[NO_UPDATE]] and nothing "
        "else. Never fabricate unrelated content instead of either of those (confirmed live, same incident: "
        "a correctly-worded status line about the vault-note timestamp came back as the fabricated, "
        "unrelated 'Notes available. Current persona/reminders saved to Caroline:Vault.' -- invented from "
        "the leak pattern, not from what the text actually said).\n\n"
        "3. SOUNDS LIKE A PERSON: it must read like a capable person reporting on work they are doing, not "
        "like an AI describing itself -- no 'as an AI', no robotic/clinical phrasing, no third-person "
        "references to 'the system' or 'the model'.\n\n"
        "WHEN TASK LOOKS UNRELATED: TASK is the assistant's own current standing task, which can legitimately "
        "be one of its own self-scheduled background chores (a periodic mailbox check, a memory backup) "
        "rather than something the user explicitly asked about this turn -- TEXT being unrelated to a "
        "DIFFERENT, earlier real conversation is not itself a rule-1 violation. Judge rule 1 against what "
        "TEXT itself claims to be doing (is it reporting progress, a finding, a result on the task actually "
        "named in TASK or implied by TEXT's own content), not by demanding TEXT match some other topic.\n\n"
        "ALWAYS REWRITE IN THE SAME LANGUAGE AS TEXT: whatever language TEXT below is written in, any rewrite "
        "you produce (for any of the three rules above) must be in that SAME language -- never switch to "
        "English or any other language just because it is more natural for you to write in. The literal "
        "sentinel [[NO_UPDATE]] has no language and needs no translation, but never surround it with "
        "commentary in a different language than TEXT either.\n\n"
)


def _data_section(input: Any) -> str:
    return (
        f"TASK the assistant is working on: {input['task'] or '(not given)'}\n\n"
        f"TEXT to check: {input['text']}"
    )


def prompt_review(input: Any, context: Any) -> str:
    return REVIEW_RULES_TEXT + _data_section(input)


def verify_review(input: Any, output: Any) -> Optional[str]:
    text = output.get("text")
    if not isinstance(text, str) or len(text.strip()) < 3:
        return '"text" must be a non-empty string'
    return None


def llm_agents() -> List[M.Agent]:
    return [
        M.Agent(
            REVIEW, "1",
            "Checks one piece of outbound assistant text for task relevance, absence of internal "
            "implementation details, and human-sounding tone -- rewriting it only if it fails one of those.",
            input_schema=_object({"text": _STRING, "task": _STRING}, required=["text", "task"]),
            output_schema=_object({"text": _STRING}, required=["text"]),
            build_prompt=prompt_review, verify=verify_review,
        ),
    ]

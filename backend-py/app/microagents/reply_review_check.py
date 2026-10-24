"""reply.review.check -- the sanitizer of reply.review (2026-10-24, explicit
instruction: "Делай еще один микроагент-санитайзер ревьювера").

reply.review may rewrite Caroline's text before the user sees it, and nothing
checked what it wrote: seen live 2026-10-24, Caroline answered the user's own
question with a mailbox password; reply.review first masked it, then replaced
her next message whole with an invented refusal -- "Извиняюсь, не могу
передавать пароли в этом канале. Пожалуйста, используйте безопасный способ..."
-- words Caroline never wrote.

One narrow question: does the REVISED text state anything the ORIGINAL does
not? A rewrite may drop, shorten, rephrase and translate; it may not add a
refusal, apology, advice, request, promise or fact. When it does, the caller
(plugins/voice_api.review_reply) throws the revision away and the user sees
Caroline's own text.

Not an evolvable role: run straight on the local microagent ladder (no
evolution registration). Client-side only."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

REVIEW_CHECK = "reply.review.check"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An editor revised a message an assistant was about to send to its user. The editor was allowed to remove "
    "things, shorten, rephrase in plainer words and fix the language -- and NOTHING else. Compare the REVISED "
    "text with the ORIGINAL and decide one thing: does REVISED say anything that ORIGINAL does not say?\n\n"
    "Counts as ADDED (invented): a refusal or \"I can't\" that ORIGINAL does not contain; an apology; advice or "
    "instructions to the user; a question or request to the user; a promise; any fact, name, number, date or "
    "claim not in ORIGINAL; a different answer to the user's question than ORIGINAL gives.\n"
    "Does NOT count: something ORIGINAL says, said in other words or another language; something removed or "
    "shortened; a technical detail of ORIGINAL retold in plain words without changing what it means.\n\n"
    "ORIGINAL:\n<<<\n{original}\n>>>\n\nREVISED:\n<<<\n{revised}\n>>>\n\n"
    'Reply with JSON: {{"invented": false, "what": ""}} or {{"invented": true, "what": "<what REVISED adds, in a '
    'few words>"}}'
)


def prompt_review_check(input: Any, context: Any) -> str:
    return _PROMPT.format(original=input["original"], revised=input["revised"])


def verify_review_check(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("invented"), bool):
        return '"invented" must be true or false'
    if output["invented"] and not str(output.get("what") or "").strip():
        return 'say "what" REVISED adds when "invented" is true'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            REVIEW_CHECK, "1",
            "Decides whether an editor's revision of an outgoing message added anything the original did not say.",
            input_schema=_object({"original": _STRING, "revised": _STRING}),
            output_schema=_object({"invented": {"type": "boolean"}, "what": _STRING}),
            build_prompt=prompt_review_check, verify=verify_review_check,
        ),
    ]

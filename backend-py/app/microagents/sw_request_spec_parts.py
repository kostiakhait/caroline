"""The formalizer split into three narrow microagents (2026-11-02, explicit instruction: "раздели формализатор на 2-3
и каждому поставь более четкое задание, ответы объединяй"). app/sw_request_spec.py runs them in parallel and renders
their answers as the one task text it always produced.

The single nano call asked for ten fields at once and filled them in: to "set the objective of this project to X" it
added "identify three test fields", "select low-cost sensors" and "create a formal project objective document" (the
akinakes coordinator then published a docCreator task nobody asked for), and an outgoing "your objective has been
captured verbatim" that the model posted and the checks passed, since the task said so.

- sw.request.work     -- who asks, what for, what to do, about whom/what, dates, conditions;
- sw.request.outgoing -- the messages the request asks to send to someone other than the user (usually none);
- sw.request.outcome  -- what has to be written or produced, what the user should learn, what they end up with.

Every action, condition and outgoing message carries the words of the request (or of the conversation before it)
that ask for it, and the code checks that those words are really there (verify_*): a step the request does not name
has no such words, the answer is refused, and the ladder tries again with the reason (its rung 2 shows the refused
reply and why). The check compares as
sw.result.facts does (case, whitespace, escapes and typography aside).

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M
from app.microagents.sw_result_facts import _norm

REQUEST_WORK = "sw.request.work"
REQUEST_OUTGOING = "sw.request.outgoing"
REQUEST_OUTCOME = "sw.request.outcome"

_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_SOURCE = (
    "CONVERSATION BEFORE THE REQUEST:\n---\n{dialogue}\n---\n\n"
    "REQUEST:\n---\n{request}\n---\n\n"
)
_DONE_NOTHING = (
    "The assistant has done nothing yet: never write that it has done, set, confirmed, sent or saved anything. "
    "Copy names, numbers, dates and quoted wording exactly, in their original language; write the rest in the "
    "request's language. "
)
_JSON = "CRITICAL: a program parses your reply. Reply with one JSON object and nothing else.\n\n"

_WORK_PROMPT = (
    _JSON
    + "Below is a request a user gave their AI assistant, with the conversation before it. Say what the request "
    "itself asks to be done -- nothing more.\n"
    '- "requester": who asks (the user, or someone the user speaks for)\n'
    '- "goal": what the request is for, in one sentence, in the request\'s own terms\n'
    '- "actions": each thing the request asks to be done, in order, as {{"do": "<the action with what it acts on '
    'and the details the request gives: whom, what, when>", "quote": "<the words of the request that ask for it, '
    'copied exactly>"}}. Only what the request asks: no preparation, research, planning, documents, '
    "confirmations or follow-ups it does not name. A request that asks for one thing has one action.\n"
    '- "targets": the people, mailboxes, files, chats or items it concerns (list)\n'
    '- "dates": dates, deadlines and times the request names, exactly as named (list)\n'
    '- "constraints": conditions the request sets, as {{"condition": "<it>", "quote": "<its words, copied '
    'exactly>"}}\n'
    "Leave a list empty when the request names nothing for it. " + _DONE_NOTHING + "\n\n"
    + _SOURCE
    + 'Reply with: {{"requester": "", "goal": "", "actions": [], "targets": [], "dates": [], "constraints": []}}'
)

_OUTGOING_PROMPT = (
    _JSON
    + "Below is a request a user gave their AI assistant, with the conversation before it. List the messages the "
    "request asks the assistant to send to someone other than the user: a letter, a chat message, a reply to "
    'someone, a note to a person or an agent. Each as {{"to": "<recipient>", "says": "<the message itself, as the '
    'recipient will read it, with the values the user gave and nothing added -- not the instruction to send it: '
    'for \\"tell Ann to come at 9\\" it says \\"Please come at 9.\\">", "quote": "<the words of the request that '
    'ask to send it, copied exactly>"}}. The assistant\'s own answer to the user is never one of these, and neither '
    "is a request that is itself a message to the assistant or to a chat room: answering it is the assistant's "
    "answer. Most requests ask for none: then the list is empty. " + _DONE_NOTHING + "\n\n"
    + _SOURCE
    + 'Reply with: {{"messages": []}}'
)

_OUTCOME_PROMPT = (
    _JSON
    + "Below is a request a user gave their AI assistant, with the conversation before it. Say what the work has "
    "to produce:\n"
    '- "content": what has to be written or produced -- the substance, with every value the user gave, copied; '
    "empty when the request produces nothing written\n"
    '- "report": what the user of THIS request should learn at the end -- describe the information their answer '
    "must give them, in terms of this request; never write the answer itself and never say that something is "
    "done, created or set: nothing is yet\n"
    '- "deliverable": what the user should have when the work is done\n'
    "Take everything from the request and the conversation; invent nothing. " + _DONE_NOTHING + "\n\n"
    + _SOURCE
    + 'Reply with: {{"content": "", "report": "", "deliverable": ""}}'
)


def _fill(template: str, input: Any) -> str:
    return template.format(dialogue=input.get("dialogue") or "(nothing yet)", request=input.get("request") or "")


def prompt_request_work(input: Any, context: Any) -> str:
    return _fill(_WORK_PROMPT, input)


def prompt_request_outgoing(input: Any, context: Any) -> str:
    return _fill(_OUTGOING_PROMPT, input)


def prompt_request_outcome(input: Any, context: Any) -> str:
    return _fill(_OUTCOME_PROMPT, input)


def _source(input: Any) -> str:
    return _norm(f'{input.get("request") or ""}\n{input.get("dialogue") or ""}')


def _quotes_check(input: Any, items: Any, key: str, what: str) -> Optional[str]:
    """Each item a {key, quote} object whose quote really is in the request or the conversation."""
    if not isinstance(items, list):
        return f'"{what}" must be a list'
    source = _source(input)
    for item in items:
        if not isinstance(item, dict) or not str(item.get(key) or "").strip():
            return f'each item of "{what}" needs a non-empty "{key}"'
        quote = _norm(str(item.get("quote") or "")).strip(" .\"'")
        if not quote or quote not in source:
            return (f'"{what}": "{str(item.get(key))[:100]}" quotes "{str(item.get("quote") or "")[:100]}", which is not in '
                    "the REQUEST or the CONVERSATION; a step the request does not ask for has no such words")
    return None


def verify_request_work(input: Any, output: Any) -> Optional[str]:
    for key in ("requester", "goal"):
        if not isinstance(output.get(key), str):
            return f'"{key}" must be a string'
    for key in ("targets", "dates"):
        if not isinstance(output.get(key), list) or not all(isinstance(x, str) for x in output[key]):
            return f'"{key}" must be a list of strings'
    return _quotes_check(input, output.get("actions"), "do", "actions") or \
        _quotes_check(input, output.get("constraints"), "condition", "constraints")


def verify_request_outgoing(input: Any, output: Any) -> Optional[str]:
    messages = output.get("messages")
    if isinstance(messages, list) and any(not str((m or {}).get("to") or "").strip() for m in messages if isinstance(m, dict)):
        return 'each message needs "to"'
    return _quotes_check(input, messages, "says", "messages")


def verify_request_outcome(input: Any, output: Any) -> Optional[str]:
    for key in ("content", "report", "deliverable"):
        if not isinstance(output.get(key), str):
            return f'"{key}" must be a string (empty when there is nothing for it)'
    return None


_INPUT = _object({"request": _STRING, "dialogue": _STRING}, ["request"])
_QUOTED = lambda key: {"type": "array", "items": _object({key: _STRING, "quote": _STRING})}  # noqa: E731


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            REQUEST_WORK, "1",
            "Who asks, what the request is for, the things it asks to be done (each with the request's words that ask "
            "for it), what it concerns, its dates and conditions.",
            input_schema=_INPUT,
            output_schema=_object({"requester": _STRING, "goal": _STRING, "actions": _QUOTED("do"), "targets": _STRINGS,
                                   "dates": _STRINGS, "constraints": _QUOTED("condition")}),
            build_prompt=prompt_request_work, verify=verify_request_work,
        ),
        M.Agent(
            REQUEST_OUTGOING, "1",
            "The messages a request asks the assistant to send to someone other than the user, each with the "
            "request's words that ask for it.",
            input_schema=_INPUT,
            output_schema=_object({"messages": {"type": "array", "items": _object({"to": _STRING, "says": _STRING,
                                                                                    "quote": _STRING})}}),
            build_prompt=prompt_request_outgoing, verify=verify_request_outgoing,
        ),
        M.Agent(
            REQUEST_OUTCOME, "1",
            "What a request has to produce: the content to write, what to report to the user, what they end up with.",
            input_schema=_INPUT,
            output_schema=_object({"content": _STRING, "report": _STRING, "deliverable": _STRING}),
            build_prompt=prompt_request_outcome, verify=verify_request_outcome,
        ),
    ]

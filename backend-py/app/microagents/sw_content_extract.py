"""sw.content.extract -- step 1 of the content-grounding check (2026-10-23): the
statements of fact and the requests that the READER of what a tool call writes
or sends will read. Split out of sw.content.grounding because one agent doing
both -- find the reader's text inside program code AND judge its facts -- kept
judging the code itself on the sw test box: a status print("saved"), a heading
level, and even the code's own doc.save(path) were flagged as unsupported
claims, and correct code was refused in a loop (explicit principle: a failing
job is split into narrow parts).

Takes the call, returns two lists -- facts, and requests (questions, asks,
offers, promises) -- nothing about code mechanics, console output or paths. Two
lists, not one: with a single "facts and requests" list the agent left out the
extra question "уточните место проведения и состав участников" in one run of
three. Step 2 (sw_content_grounding.py) judges exactly these lists.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

CONTENT_EXTRACT = "sw.content.extract"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant is about to make a tool call that writes or sends something: a document, a file, a "
    "letter, a message. The call may be program code. Find the text a PERSON will read in the result -- the "
    "words in the document's paragraphs, headings and table cells, the subject and body of the letter, the "
    "message text -- and fill two lists, one short sentence per item, in the language of the text:\n"
    "1. facts -- every STATEMENT OF FACT: claims about the world or the data (figures and what they total, "
    "comparisons, causes, trends, dates, names).\n"
    "2. requests -- everything the text ASKS of its reader or anyone else, or OFFERS or PROMISES: every "
    "question put to them (\"where?\", \"who is coming?\", \"уточните ...\"), every request (\"please "
    "confirm\", \"let me know\"), every invitation, offer, or promise to do something. Go through the text "
    "sentence by sentence and put each such sentence here; write it as \"asks the reader to ...\", \"asks "
    "...?\", \"promises to ...\".\n"
    "A request, question or promise that the text only REPORTS or quotes -- a summary saying that someone "
    "else asked something, or what a letter or chat said -- is a fact about that conversation: put it in "
    "facts, not in requests. requests holds only what the text itself, in its own voice, asks of its reader.\n"
    "Do NOT list: anything about the program (library calls and their parameters, heading levels, styles, "
    "variable names, file paths and where the file is saved); text the program only prints to the console as a "
    "status message; plain labels (column headers, \"Итого\", \"Отчёт за 3 квартал\" as a title); greetings and "
    "sentence structure. A list stays empty when the reader's text has nothing for it.\n\n"
    "THE CALL:\n{tool}({args})\n\n"
    'Reply with a JSON object: {{"facts": ["<fact>", ...], "requests": ["<request>", ...]}}'
)


def prompt_content_extract(input: Any, context: Any) -> str:
    return _PROMPT.format(tool=input.get("tool") or "", args=input.get("args") or "")


def verify_content_extract(input: Any, output: Any) -> Optional[str]:
    for key in ("facts", "requests"):
        items = output.get(key)
        if not isinstance(items, list) or not all(isinstance(f, str) and f.strip() for f in items):
            return f'"{key}" must be a list of non-empty strings (empty when there is nothing for it)'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            CONTENT_EXTRACT, "3",
            "Lists the statements of fact, and separately the requests (questions, asks, offers, promises), in the "
            "text a reader will read in what a tool call writes or sends, ignoring code mechanics, console output "
            "and file paths.",
            input_schema=_object({"tool": _STRING, "args": _STRING}),
            output_schema=_object({"facts": {"type": "array", "items": _STRING}, "requests": {"type": "array", "items": _STRING}}),
            build_prompt=prompt_content_extract, verify=verify_content_extract,
        ),
    ]

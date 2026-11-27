"""reply.gender -- grammatical gender of the speaker in an outgoing reply (2026-11-27, the owner's command after a live
check: Caroline, a woman, wrote "я заменил траву и деревья" in a Russian reply).

One narrow job: in the languages where a verb, participle or adjective that refers to the speaker herself or himself
carries a gender (Russian "заменила/заменил", Polish, Hebrew, Arabic ...), put those words in the persona's gender and change
NOTHING else. The caller (plugins/voice_api.fix_gender) throws the answer away when it differs from the original by more than
word endings. Not an evolvable role: run straight on the local microagent ladder. Client-side only."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

GENDER = "reply.gender"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "The speaker of the message below is {gender}. In languages where a verb (usually the past tense), a participle "
    "or an adjective that refers to the speaker herself or himself changes with gender (Russian: \"я заменил\" / \"я заменила\", "
    "\"я рада\" / \"я рад\", \"я готов\" / \"я готова\"), every such word must be in the {gender} form. Fix those words and "
    "ONLY those: do not touch words about other people or things, quotations, code, file names, numbers, punctuation, order or "
    "anything else; do not translate, shorten or add. A language in which these words have no gender, or a text with no such "
    "word, or one already right, comes back exactly as it is.\n\n"
    "MESSAGE:\n<<<\n{text}\n>>>\n\n"
    'Reply with JSON: {{"text": "<the message, with only those words fixed>"}}'
)


def prompt_gender(input: Any, context: Any) -> str:
    return _PROMPT.format(gender=input["gender"], text=input["text"])


def verify_gender(input: Any, output: Any) -> Optional[str]:
    if not str(output.get("text") or "").strip():
        return '"text" must be the whole message'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            GENDER, "1",
            "Puts the words that refer to the speaker (past-tense verbs, adjectives) in the speaker's grammatical gender.",
            input_schema=_object({"text": _STRING, "gender": _STRING}),
            output_schema=_object({"text": _STRING}),
            build_prompt=prompt_gender, verify=verify_gender,
        ),
    ]

"""consult -- ports backend/src/consultTools.ts's consult_large_model tool.
Asks a GPT-5-class model (Camerlengo's "LARGE" model category) for wording
advice, gated on the user being logged into their own SquirrelWisdom
account -- reuses notes_api.py's shared credentials file (the SAME account
backend/src/login.ts's isLoggedIn()/getV2Session() check, just reached
from Python).

Login gate goes through sw_gate.py's shared require_sw_or_prompt -- the
native login window auto-opens on the first refusal since the last
logout (see that module's own docstring), same as every other SW-gated
tool.
"""

from __future__ import annotations

from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.notes_api import load_credentials, verify_password
from app.plugins.sw_api import CAROLINE_SW_KEY, call_v2
from app.session_context import get_send
from app.sw_gate import require_sw_or_prompt


async def consult_large_model(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    gate = await require_sw_or_prompt(get_send())
    if not gate.ok:
        return {"text": gate.message, "is_error": True}
    creds = load_credentials()
    assert creds is not None  # gate.ok guarantees this
    session = await verify_password(creds["email"], creds["password"])
    result = await call_v2("ai:resolve", key=CAROLINE_SW_KEY, question=args["question"], model="LARGE", session=session)
    advice = result.get("result")
    if not isinstance(advice, str):
        raise RuntimeError("consult_large_model (ai:resolve) failed: no result text returned")
    return {"text": advice}


def _usage_instructions() -> str:
    return (
        "You have a consult_large_model tool that asks a GPT-5-class model for wording advice. Use it when a "
        "legal, commercial, or social (non-technical) question is, by your own judgment, both high-complexity "
        "and high-importance -- something the user will act on, sign, send to another real person, or that "
        "carries real legal/financial/relationship consequences. GPT-5-class models are measurably better at "
        "this kind of careful, nuanced non-technical phrasing than you are; you remain the better one at code "
        "and technical execution, so keep doing those yourself without consulting anyone. Don't reach for this "
        "on routine, low-stakes, or clearly technical questions -- it costs a real extra call and most things "
        "don't need it. What it returns is advice for YOU to weigh and fold into your own final answer, never "
        "a response to just relay verbatim -- you're still the one deciding what to actually say and taking "
        "responsibility for it. It only works when the user is logged into their own SquirrelWisdom account; if "
        "it comes back unavailable, just proceed on your own judgment as you would have before this tool existed."
    )


PLUGIN = Plugin(
    name="consult",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "consult_large_model",
            "Asks a more capable, GPT-5-class model for advice on WORDING a response to a legal, commercial, "
            "or social (non-technical) question -- use this when such a question is, by your own judgment, "
            "both high-complexity AND high-importance (something the user will act on, sign, send to someone "
            "else, or that carries real legal/financial/relationship consequences). This returns ADVICE for "
            "you to weigh and incorporate into your own final answer -- it does not replace your own response "
            "or speak directly to the user; you decide what to actually say. Only available when the user is "
            "logged into their own SquirrelWisdom account (returns an error otherwise -- if that happens, "
            "just proceed using your own judgment, same as before this tool existed).",
            {
                "question": str,
            }, consult_large_model,
        ),
    ],
)

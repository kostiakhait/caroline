"""The usage-log tag (usage_extra) reaches resolve_agentic only when Camerlengo's AI.py takes it.
Run: python -m pytest test_usage_extra.py -q"""
import asyncio

from app import small_model_engine as sme


class NewAI:
    def __init__(self):
        self.kwargs = None

    def resolve_agentic(self, messages, tool_defs, executor_fn, model=None, app=None, usage_extra=None):
        self.kwargs = {"model": model, "app": app, "usage_extra": usage_extra}
        return "done"


class OldAI:
    def __init__(self):
        self.kwargs = None

    def resolve_agentic(self, messages, tool_defs, executor_fn, model=None, app=None):
        self.kwargs = {"model": model, "app": app}
        return "done"


def _run(ai):
    return asyncio.run(sme._resolve_agentic_with_watchdog(
        ai, sme._TurnTracker(), messages=[], tool_defs=None, executor_fn=lambda name, args: "",
        model="m", app="caroline", usage_extra={"purpose": "tier", "tier": 0},
    ))


def test_the_tag_goes_to_an_ai_that_takes_it():
    ai = NewAI()
    assert _run(ai) == "done"
    assert ai.kwargs["usage_extra"] == {"purpose": "tier", "tier": 0}


def test_an_older_ai_gets_the_call_without_the_tag():
    ai = OldAI()
    assert _run(ai) == "done"
    assert ai.kwargs == {"model": "m", "app": "caroline"}

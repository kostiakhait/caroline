"""run_small_model_turn's request_text (2026-11-02): when a caller wraps the request in instructions of its own (an
akinakes turn), the formalizer reads the requester's words, not the whole text the model is prompted with; without
request_text nothing changes.
Run: python -m pytest test_request_text.py -q"""

import asyncio

import pytest

pytest.importorskip("claude_agent_sdk")
from app import small_model_engine as sme  # noqa: E402
from app import sw_request_spec  # noqa: E402


@pytest.fixture
def formalized(monkeypatch):
    seen: list[str] = []

    def fake_formalize(request, recent_dialogue_lines, tab_id=None):
        seen.append(request)

        async def nothing():
            return None
        return nothing()

    async def no_model():
        return None  # the turn ends right after the formalizer has been started

    monkeypatch.setattr(sw_request_spec, "formalize", fake_formalize)
    monkeypatch.setattr(sme, "_model_ai", no_model)
    return seen


def test_the_formalizer_reads_the_requester_words_when_given(formalized):
    text = "[Agent Bus chat message ...] Hand it to the room agents.\n\nalice wrote: Set the objective to X."
    asyncio.run(sme.run_small_model_turn("tab", ".", None, text, [], "en", None, request_text="alice wrote: Set the objective to X."))
    assert formalized == ["alice wrote: Set the objective to X."]


def test_without_request_text_the_user_text_is_the_request(formalized):
    asyncio.run(sme.run_small_model_turn("tab", ".", None, "Set the objective to X.", [], "en", None))
    assert formalized == ["Set the objective to X."]

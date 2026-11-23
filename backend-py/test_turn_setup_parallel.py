"""The sw turn's setup runs side by side (2026-11-23): the one-word complexity call starts with the request spec, not
after it -- it read only the request and waited 3.5 s median for the spec. Run: python -m pytest test_turn_setup_parallel.py -q"""

import asyncio
import time

import pytest

pytest.importorskip("claude_agent_sdk")
from app import small_model_engine as sme  # noqa: E402
from app import sw_request_spec  # noqa: E402
from app.persona import get_persona  # noqa: E402


class _Stop(BaseException):
    """Ends the turn where the test has what it needs (not an Exception: no handler on the way swallows it)."""


def test_the_complexity_call_starts_before_the_request_spec_is_done(monkeypatch, tmp_path):
    marks: dict[str, float] = {}

    def slow_formalize(request, recent_dialogue_lines, tab_id=None):
        async def spec():
            await asyncio.sleep(0.2)
            marks["spec_done"] = time.monotonic()
            return None
        return spec()

    async def complexity(ai, cfg, user_text):
        marks["complexity_started"] = time.monotonic()
        raise _Stop

    async def nothing(*args, **kwargs):
        return None

    async def no_advice(*args, **kwargs):
        return [], None

    async def fake_ai():
        return object()

    monkeypatch.setattr(sme, "get_model_provider_key", nothing)
    monkeypatch.setattr(sw_request_spec, "formalize", slow_formalize)
    monkeypatch.setattr(sme, "_model_ai", fake_ai)
    monkeypatch.setattr(sme.rung_advice, "advise", nothing)
    monkeypatch.setattr(sme, "_tags_and_tier_advice", no_advice)
    monkeypatch.setattr(sme, "_start_tier_index", complexity)

    with pytest.raises(_Stop):
        asyncio.run(sme.run_small_model_turn("tab", str(tmp_path), get_persona(str(tmp_path)), "Find the cheapest flight to Kyiv next week", [], "en", None))
    assert marks["complexity_started"] < marks["spec_done"]


def test_a_start_decided_by_the_fast_lane_asks_no_complexity_call(monkeypatch, tmp_path):
    called = []

    async def complexity(ai, cfg, user_text):
        called.append(user_text)
        return 0

    async def nothing(*args, **kwargs):
        return None

    async def stop_after_setup(*args, **kwargs):
        raise _Stop

    async def fake_ai():
        return object()

    monkeypatch.setattr(sme, "get_model_provider_key", nothing)
    monkeypatch.setattr(sw_request_spec, "formalize", lambda *a, **k: nothing())
    monkeypatch.setattr(sme, "_model_ai", fake_ai)
    monkeypatch.setattr(sme.rung_advice, "advise", nothing)
    monkeypatch.setattr(sme, "_tags_and_tier_advice", stop_after_setup)
    monkeypatch.setattr(sme, "_start_tier_index", complexity)

    with pytest.raises(_Stop):
        asyncio.run(sme.run_small_model_turn("tab", str(tmp_path), get_persona(str(tmp_path)), "Find flights", [], "en", None, start_tier=1))
    assert called == []

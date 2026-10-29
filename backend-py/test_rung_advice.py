"""The start rung decided by a model (app/rung_advice.py, app/microagents/turn_rung_advice.py) and its wiring
into the Claude ladder, Codex's effort and the sw cascade. Run: python -m pytest test_rung_advice.py -q"""

import asyncio

import pytest

from app import rung_advice as ra
from app import small_model_engine as sme
from app.engines.codex_engine import CodexEngine, CodexOptions
from app.engines.codex_rpc import CodexRpcError
from app.microagents import turn_rung_advice as tra
from test_claude_ladder import Session, result_msg, run, text_wire, user_item, verdicts


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr("app.chat_session_mixins.ladder.get_model_override", lambda ws, provider: None)
    monkeypatch.delenv("CAROLINE_NO_LADDER", raising=False)
    ra._memory.clear()
    return tmp_path


def advice(monkeypatch, *answers):
    """advise() replaced: returns the given answers in turn; the calls are recorded."""
    calls, queue = [], list(answers)

    async def fake(engine, ladder, request, dialogue, ws, tab, reaction=None):
        calls.append({"engine": engine, "ladder": [m for m, _ in ladder], "request": request, "reaction": reaction})
        return queue.pop(0) if queue else (None, "")

    monkeypatch.setattr(ra, "advise", fake)
    return calls


async def begin_and_yield(s, item):
    s._rung_advice_begin(item)
    await s._ladder_before_yield(item)


# --- the microagent --------------------------------------------------------------

def test_verify():
    ladder = [{"model": "haiku"}, {"model": "sonnet"}, {"model": "opus"}]
    assert tra.verify_rung_advice({"ladder": ladder}, {"start_rung": 2, "why": "a court matter"}) is None
    assert tra.verify_rung_advice({"ladder": ladder}, {"start_rung": 3, "why": "x"})
    assert tra.verify_rung_advice({"ladder": ladder}, {"start_rung": True, "why": "x"})
    assert tra.verify_rung_advice({"ladder": ladder}, {"start_rung": "1", "why": "x"})
    assert tra.verify_rung_advice({"ladder": ladder}, {"start_rung": 0, "why": " "})


def test_prompt_shows_ladder_history_and_reaction():
    text = tra.prompt_rung_advice({
        "request": "read the court documents", "dialogue": "User: hi", "reaction": -0.7,
        "ladder": [{"model": "haiku", "good_for": "small"}, {"model": "opus", "good_for": "big"}],
        "history": [{"ago": "3 min", "engine": "claude", "request": "the order", "started": "haiku", "ended": "sonnet",
                     "escalation_notes": ["haiku: facts not in the tool output"]}],
    }, None)
    assert "0: haiku -- small" in text and "1: opus -- big" in text
    assert "started on haiku, ended on sonnet" in text and "facts not in the tool output" in text
    assert "-0.70" in text and "read the court documents" in text
    assert "unknown" in tra.prompt_rung_advice({"request": "x", "ladder": []}, None)


# --- the history -----------------------------------------------------------------

def test_history_is_a_ring_mirrored_to_disk(isolated):
    ws = str(isolated)
    for i in range(ra.HISTORY_KEPT + 3):
        ra.record(ws, "t1", "claude", f"request {i}", ra.CLAUDE_LADDER, 0, 1, ["haiku: missing"])
    history = ra.recent(ws, "t1")
    assert len(history) == ra.HISTORY_KEPT and history[-1]["request"] == f"request {ra.HISTORY_KEPT + 2}"
    assert history[-1]["started"] == "haiku" and history[-1]["ended"] == "sonnet"
    ra._memory.clear()                                        # a restart
    assert ra.recent(ws, "t1") == history
    assert ra.recent(ws, "other") == []


def test_history_failure_never_raises():
    ra.record("Z:/does/not/exist", "t", "sw", "x", ra.CLAUDE_LADDER, 0, 0)


# --- advise() --------------------------------------------------------------------

def local_agent(monkeypatch, output=None, error=None, delay=0.0):
    seen = []

    async def fake(agent, module, agent_input):
        seen.append(agent_input)
        await asyncio.sleep(delay)
        if error:
            raise error
        return {"status": "ok", "output": output}

    monkeypatch.setattr("app.plugins.voice_api._run_local_microagent", fake)
    return seen


def test_advise_returns_the_step_and_passes_history_and_reaction(monkeypatch, isolated):
    ws = str(isolated)
    ra.record(ws, "t", "claude", "the court order", ra.CLAUDE_LADDER, 0, 1, ["haiku: invented facts"])
    seen = local_agent(monkeypatch, {"start_rung": 2, "why": "legal matter, previous answer was corrected"})

    async def go():
        reaction = asyncio.get_running_loop().create_future()
        reaction.set_result(-0.8)
        return await ra.advise("claude", ra.CLAUDE_LADDER, "you got it wrong", "User: ...", ws, "t", reaction)

    assert run(go()) == (2, "legal matter, previous answer was corrected")
    assert seen[0]["reaction"] == -0.8 and seen[0]["history"][0]["ended"] == "sonnet"
    assert [s["model"] for s in seen[0]["ladder"]] == ["haiku", "sonnet", "opus"]


def test_advise_failure_out_of_range_and_timeout_mean_no_advice(monkeypatch, isolated):
    ws = str(isolated)
    local_agent(monkeypatch, error=RuntimeError("down"))
    assert run(ra.advise("sw", ra.CLAUDE_LADDER, "x", "", ws, "t")) == (None, "")
    local_agent(monkeypatch, {"start_rung": 7, "why": "x"})
    assert run(ra.advise("sw", ra.CLAUDE_LADDER, "x", "", ws, "t"))[0] is None
    monkeypatch.setattr(ra, "ADVICE_TIMEOUT_S", 0.05)
    local_agent(monkeypatch, {"start_rung": 1, "why": "x"}, delay=1.0)
    assert run(ra.advise("sw", ra.CLAUDE_LADDER, "x", "", ws, "t")) == (None, "")


def test_reaction_not_ready_in_time_is_unknown(monkeypatch, isolated):
    monkeypatch.setattr(ra, "REACTION_WAIT_S", 0.05)
    seen = local_agent(monkeypatch, {"start_rung": 0, "why": "small talk"})

    async def go():
        never = asyncio.get_running_loop().create_future()
        return await ra.advise("claude", ra.CLAUDE_LADDER, "hi", "", str(isolated), "t", never)

    assert run(go())[0] == 0 and seen[0]["reaction"] is None


# --- Claude --------------------------------------------------------------------

def test_claude_turn_starts_on_the_advised_rung(monkeypatch):
    s = Session()
    calls = advice(monkeypatch, (1, "documents of a court case"))
    run(begin_and_yield(s, user_item("read the documents and tell me my prospects")))
    assert s.client.models == ["sonnet"] and s._ladder.rung == 1 and s._ladder.start_rung == 1
    assert calls[0]["engine"] == "claude" and calls[0]["ladder"] == ["haiku", "sonnet", "opus"]


def test_claude_no_advice_starts_on_haiku(monkeypatch):
    s = Session()
    advice(monkeypatch, (None, ""))
    run(begin_and_yield(s, user_item("what's the weather")))
    assert s.client.models == ["haiku"]


def test_claude_advised_top_rung_is_not_judged(monkeypatch):
    s = Session()
    advice(monkeypatch, (2, "legal"))
    judged = verdicts(monkeypatch)

    async def go():
        await begin_and_yield(s, user_item())
        assert s._ladder_hold(text_wire("opus answer")) is False
        return await s._ladder_on_result(result_msg("opus answer"))

    assert run(go()) is False and judged == [] and s.client.models == ["opus"]


def test_pinned_model_internal_notes_and_continuations_are_not_advised(monkeypatch):
    s = Session()
    calls = advice(monkeypatch)
    monkeypatch.setattr("app.chat_session_mixins.ladder.get_model_override", lambda ws, p: "opus")
    run(begin_and_yield(s, user_item()))
    monkeypatch.setattr("app.chat_session_mixins.ladder.get_model_override", lambda ws, p: None)
    internal = user_item("[Internal: a reminder]")
    internal["memory_advice_for"] = None
    run(begin_and_yield(s, internal))
    cont = user_item("go on")
    cont["ladder_continuation"] = True
    run(begin_and_yield(s, cont))
    assert calls == []


def test_settled_claude_turn_goes_to_the_history(monkeypatch, isolated):
    s = Session()
    s.workspace_dir = str(isolated)
    advice(monkeypatch, (0, "a lookup"))
    verdicts(monkeypatch, ("up", "facts not in the tool output"), ("done", ""))

    async def go():
        await begin_and_yield(s, user_item("what did the order decide"))
        s._ladder_hold(text_wire("haiku draft"))
        assert await s._ladder_on_result(result_msg("haiku draft")) is True
        await s._ladder_before_yield(s.queue.pop(0))
        s._ladder_hold(text_wire("sonnet answer"))
        assert await s._ladder_on_result(result_msg("sonnet answer")) is False

    run(go())
    [entry] = ra.recent(str(isolated), "t")
    assert entry["engine"] == "claude" and entry["started"] == "haiku" and entry["ended"] == "sonnet"
    assert "facts not in the tool output" in entry["escalation_notes"][0]


def test_message_joining_a_judged_turn_drops_its_advice(monkeypatch):
    s = Session()
    advice(monkeypatch, (0, "x"), (2, "y"))

    async def go():
        await begin_and_yield(s, user_item("first"))
        s._ladder_hold(text_wire("held"))
        second = user_item("second")
        s._rung_advice_begin(second)
        task = second["rung_advice_task"]
        await s._ladder_before_yield(second)
        await asyncio.sleep(0)
        return task

    task = run(go())
    assert task.cancelled() and s.client.models == ["haiku"]


# --- Codex -----------------------------------------------------------------------

def test_codex_effort_follows_the_advice_and_is_put_back(monkeypatch, isolated):
    s = Session()
    s.engine_kind, s.workspace_dir = "openai", str(isolated)
    calls = advice(monkeypatch, (2, "legal"), (None, ""), (0, "small talk"))
    first, second, third = user_item("court papers"), user_item("thanks"), user_item("weather?")
    run(begin_and_yield(s, first))
    assert first["message"]["codex_effort"] == "xhigh" and calls[0]["engine"] == "codex"
    run(s._ladder_on_result(result_msg()))
    run(begin_and_yield(s, second))
    assert second["message"]["codex_effort"] == "medium"       # Codex keeps an effort: put back explicitly
    run(begin_and_yield(s, third))
    assert "codex_effort" not in third["message"]               # already on the default
    assert s.client.models == []                                 # no model switching on Codex
    [entry] = ra.recent(str(isolated), "t")
    assert entry["engine"] == "codex" and entry["started"] == "xhigh"


class FakeRpc:
    def __init__(self, reject_effort=False):
        self.calls, self.reject_effort = [], reject_effort

    async def request(self, method, params, timeout=None):
        self.calls.append((method, params))
        if self.reject_effort and "effort" in params:
            raise CodexRpcError(-32602, "unsupported effort")
        return {}


def test_codex_turn_start_carries_the_effort_and_survives_a_rejection():
    engine = CodexEngine(CodexOptions(codex_exe="x", codex_home="h", cwd="."))
    engine._thread_id = "th"
    engine._rpc = FakeRpc()
    run(engine._start_turn([{"type": "text", "text": "hi"}], "high"))
    assert engine._rpc.calls == [("turn/start", {"threadId": "th", "input": [{"type": "text", "text": "hi"}], "effort": "high"})]
    engine._rpc = FakeRpc(reject_effort=True)
    run(engine._start_turn([{"type": "text", "text": "hi"}], "xhigh"))
    assert [("effort" in p) for _, p in engine._rpc.calls] == [True, False]
    engine._rpc = FakeRpc()
    run(engine._start_turn([{"type": "text", "text": "hi"}]))
    assert "effort" not in engine._rpc.calls[0][1]


# --- sw --------------------------------------------------------------------------

def test_sw_start_is_the_highest_of_the_three():
    assert sme._raised_by_rung_advice(1, None, "t") == 1
    assert sme._raised_by_rung_advice(2, 1, "t") == 2          # only ever raises
    assert sme._raised_by_rung_advice(0, 3, "t") == 3

"""The model ladder of a Claude tab (app/claude_ladder.py + chat_session_mixins/ladder.py).
Run: python -m pytest test_claude_ladder.py -q"""

import asyncio
from types import SimpleNamespace

import pytest

from app import claude_ladder as cl
from app import small_model_engine as sme
from app.chat_session_mixins.ladder import LadderMixin


def text_wire(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def tool_wire(call_id="t1"):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": call_id, "name": "Bash", "input": {"c": 1}}]}}


def result_msg(text="answer", is_error=False):
    return SimpleNamespace(result=text, is_error=is_error, usage={"input_tokens": 3})


class FakeClient:
    def __init__(self, fail=False):
        self.models, self.fail = [], fail

    async def set_model(self, model):
        if self.fail:
            raise RuntimeError("no")
        self.models.append(model)


class Session(LadderMixin):
    """Just the parts of ChatSession the ladder touches."""

    def __init__(self, client=None):
        self.tab_id, self.workspace_dir, self.engine_kind = "t", "ws", "claude"
        self.client = client or FakeClient()
        self.queue, self.sent = [], []
        self._queue_event = asyncio.Event()
        self.turn_is_voice = False
        self.pending_is_real_user = True
        self.hang_interrupt_result_pending = self.ignore_next_result_recovery = False
        self.forced_compaction_result_pending = self.clear_tab_result_pending = False
        self.real_user_turn_answered = False
        self.turn_saw_any_visible_text = False
        self.consecutive_narration_count = 0
        self.last_visible_output_at = 0
        self._turn_last_visible_text = ""

    async def send(self, envelope):
        self.sent.append(envelope)

    async def _translate_wire_visible_text(self, wire):
        return wire


@pytest.fixture(autouse=True)
def no_override(monkeypatch):
    monkeypatch.setattr("app.chat_session_mixins.ladder.get_model_override", lambda ws, provider: None)
    monkeypatch.delenv("CAROLINE_NO_LADDER", raising=False)


def verdicts(monkeypatch, *answers):
    calls = []
    queue = list(answers)

    async def fake(user_text, candidate, trace, can_escalate, tab_id):
        calls.append((user_text, candidate, list(trace), can_escalate))
        return queue.pop(0)

    monkeypatch.setattr(sme, "verify_claude_attempt", fake)
    return calls


def user_item(text="do it"):
    return {"message": {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "[Sent: now]"}, {"type": "text", "text": text}]}, "parent_tool_use_id": None},
        "is_voice": False, "memory_advice_for": text}


def run(coro):
    return asyncio.run(coro)


# --- pure parts ---------------------------------------------------------------

def test_user_text_skips_sent_line_and_markers():
    assert cl.user_text_of(user_item("hello")["message"]) == "hello"


def test_turn_trace_and_hold():
    turn = cl.LadderTurn(user_text="x")
    turn.observe(tool_wire("a"))
    turn.observe({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "out"}]}})
    assert turn.trace and "Bash" in turn.trace[0] and "out" in turn.trace[0]
    assert turn.should_hold(text_wire("hi")) and not turn.should_hold(tool_wire())
    turn.next_rung("why")
    assert turn.model == "sonnet" and turn.trace == [] and not turn.is_top
    turn.next_rung("again")
    assert turn.is_top


def test_needs_verdict():
    turn = cl.LadderTurn(user_text="x")
    assert cl.needs_verdict(turn, "answer", False)
    assert not cl.needs_verdict(turn, "answer", True)
    assert not cl.needs_verdict(turn, "[[NO_UPDATE]]", False)
    turn.rung = 2
    assert not cl.needs_verdict(turn, "answer", False)


def test_ladder_enabled():
    assert cl.ladder_enabled(None, "claude")
    assert not cl.ladder_enabled("opus", "claude")
    assert not cl.ladder_enabled(None, "openai")
    assert not cl.ladder_enabled(None, "claude", False)


def test_read_verdict():
    assert sme.read_claude_attempt_verdict("OK", True) == ("done", "")
    assert sme.read_claude_attempt_verdict("", True) == ("done", "")
    action, note = sme.read_claude_attempt_verdict(f"{sme.ESCALATION_SENTINEL} missing the file", True)
    assert action == "up" and "missing" in note
    assert sme.read_claude_attempt_verdict(f"{sme.ESCALATION_SENTINEL} x", False)[0] == "done"


# --- the wiring ---------------------------------------------------------------

def test_turn_starts_on_haiku_and_done_flushes_held(monkeypatch):
    s = Session()
    verdicts(monkeypatch, ("done", ""))

    async def go():
        await s._ladder_before_yield(user_item())
        assert s.client.models == ["haiku"]
        s._ladder_observe(text_wire("draft"))
        assert s._ladder_hold(text_wire("draft"))
        assert not s._ladder_hold(tool_wire())
        assert s.sent == []
        swallowed = await s._ladder_on_result(result_msg("draft"))
        return swallowed

    assert run(go()) is False
    assert [e["message"]["message"]["content"][0]["text"] for e in s.sent] == ["draft"]
    assert s._ladder.active is False


def test_up_swallows_result_switches_model_and_queues_continuation(monkeypatch):
    s = Session()
    calls = verdicts(monkeypatch, ("up", "file missing"), ("up", "still missing"), ("done", ""))

    async def go():
        await s._ladder_before_yield(user_item())
        s._ladder_hold(text_wire("haiku draft"))
        assert await s._ladder_on_result(result_msg("haiku draft")) is True
        assert s.client.models == ["haiku", "sonnet"] and s.sent == [] and s._ladder.held == []
        cont = s.queue[0]
        assert cont["ladder_continuation"] and "file missing" in cont["message"]["message"]["content"][0]["text"]
        s.queue.pop(0)
        await s._ladder_before_yield(cont)           # the continuation does not restart the ladder
        assert s._ladder.model == "sonnet" and s.client.models == ["haiku", "sonnet"]
        s._ladder_hold(text_wire("sonnet draft"))
        assert await s._ladder_on_result(result_msg("sonnet draft")) is True
        assert s.client.models[-1] == "opus"
        s.queue.pop(0)
        assert s._ladder_hold(text_wire("opus answer")) is False   # the top rung is not held
        assert await s._ladder_on_result(result_msg("opus answer")) is False

    run(go())
    assert [c[3] for c in calls] == [True, True]       # the top rung is never judged
    assert s._ladder.escalations and not s._ladder.active


def test_error_result_and_silent_turn_are_not_judged(monkeypatch):
    s = Session()
    calls = verdicts(monkeypatch)

    async def go():
        await s._ladder_before_yield(user_item())
        assert await s._ladder_on_result(result_msg("boom", is_error=True)) is False
        await s._ladder_before_yield(user_item())
        s._ladder_hold(text_wire("[[NO_UPDATE]]"))
        assert await s._ladder_on_result(result_msg("[[NO_UPDATE]]")) is False

    run(go())
    assert calls == []


def test_fake_results_pass_through_untouched(monkeypatch):
    s = Session()
    calls = verdicts(monkeypatch)

    async def go():
        await s._ladder_before_yield(user_item())
        s.forced_compaction_result_pending = True
        assert await s._ladder_on_result(result_msg()) is False

    run(go())
    assert calls == [] and s._ladder.active


def test_pinned_model_or_other_engine_means_no_ladder(monkeypatch):
    s = Session()
    monkeypatch.setattr("app.chat_session_mixins.ladder.get_model_override", lambda ws, p: "opus")
    run(s._ladder_before_yield(user_item()))
    assert s._ladder is None and s.client.models == []
    monkeypatch.setattr("app.chat_session_mixins.ladder.get_model_override", lambda ws, p: None)
    monkeypatch.setenv("CAROLINE_NO_LADDER", "1")
    run(s._ladder_before_yield(user_item()))
    assert s._ladder is None


def test_set_model_failure_falls_back_to_no_ladder():
    s = Session(FakeClient(fail=True))
    run(s._ladder_before_yield(user_item()))
    assert s._ladder is None
    assert run(s._ladder_on_result(result_msg())) is False


def test_verifier_failure_means_done(monkeypatch):
    s = Session()

    async def boom(*a, **k):
        raise RuntimeError("verifier down")

    monkeypatch.setattr(sme, "verify_claude_attempt", boom)

    async def go():
        await s._ladder_before_yield(user_item())
        s._ladder_hold(text_wire("draft"))
        return await s._ladder_on_result(result_msg("draft"))

    assert run(go()) is False and len(s.sent) == 1


def test_reset_clears_state():
    s = Session()
    run(s._ladder_before_yield(user_item()))
    s._ladder_reset()
    assert s._ladder is None and s._ladder_model is None


def test_pinning_a_model_moves_a_client_the_ladder_had_switched(monkeypatch):
    s = Session()
    run(s._ladder_before_yield(user_item()))              # Auto: the ladder puts the client on haiku
    assert s.client.models == ["haiku"]
    monkeypatch.setattr("app.chat_session_mixins.ladder.get_model_override", lambda ws, p: "opus")
    run(s._ladder_before_yield(user_item()))              # the user picked Opus in Settings
    assert s._ladder is None and s.client.models == ["haiku", "opus"]
    run(s._ladder_before_yield(user_item()))              # and it stays there, no repeated switch
    assert s.client.models == ["haiku", "opus"]


def test_message_during_a_judged_turn_joins_it_and_keeps_held_replies(monkeypatch):
    s = Session()
    verdicts(monkeypatch, ("done", ""))

    async def go():
        await s._ladder_before_yield(user_item("first"))
        s._ladder_hold(text_wire("held draft"))
        await s._ladder_before_yield(user_item("second"))     # arrives before the result
        assert len(s._ladder.held) == 1 and "second" in s._ladder.user_text
        await s._ladder_on_result(result_msg("held draft"))

    run(go())
    assert len(s.sent) == 1


def test_verifier_prompt_lets_a_permitted_question_through_for_a_claude_attempt():
    tracker = sme._TurnTracker()
    claude = sme._build_verification_prompt(tracker, "Send it? Here is the text.", True, ["- Bash({}) -> ok"], claude_attempt=True)
    assert "NOT unfinished work" in claude and "Never OK a reply that asks the user something" not in claude
    cascade = sme._build_verification_prompt(tracker, "Send it?", True, ["- x -> ok"])
    assert "Never OK a reply that asks the user something" in cascade and "NOT unfinished work" not in cascade

"""The hang watchdog (chat_session_mixins/watchdog.py, _check_hang): a running tool call and a
message joining a running turn are not an "empty turn"; an interrupt tells the model it was
automatic, and why. Run: python -m pytest test_hang_watchdog.py -q"""

import asyncio
import time
from types import SimpleNamespace

from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from app.chat_session_mixins import watchdog
from app.chat_session_mixins.watchdog import WatchdogMixin


class Monitor:
    available = True
    last_cpu_percent = last_rss_delta = last_io_delta = 0

    def sample(self):
        return True

    def seconds_since_last_activity(self):
        return 0.0  # the process looks alive: only the empty-turn ceiling can fire


class Client:
    def __init__(self):
        self.interrupts = 0

    async def interrupt(self):
        self.interrupts += 1


class Tab(WatchdogMixin):
    def __init__(self, turn_age_s):
        self.tab_id, self.workspace_dir, self.ended = "hang-test", "ws", False
        self.turn_pending = True
        self.turn_pending_since = time.monotonic() - turn_age_s
        self.connecting_since = None
        self.has_seen_init = True
        self.turn_saw_any_visible_text = False
        self.turn_tool_outcomes = {}
        self._pending_tool_calls = {}
        self._open_tool_calls = {}
        self.turn_joined_running = False
        self.hang_count = 0
        self.hang_interrupted_at = None
        self.hang_interrupt_result_pending = False
        self.hang_interrupt_reason = None
        self.hang_interrupted_tool_name = None
        self.hang_interrupted_tool_elapsed_s = None
        self.last_activity = time.monotonic()
        self.last_tool_use_name = None
        self.last_tool_use_started_at = None
        self._process_activity_monitor = Monitor()
        self.client = Client()
        self.injected = []

    def inject_proactive(self, text, *a, **k):
        self.injected.append(text)
        return True


def check(monkeypatch, tab):
    monkeypatch.setattr(watchdog, "load_chat_mode", lambda *a: "claude")
    asyncio.run(tab._check_hang())


def test_an_empty_turn_is_interrupted_and_the_model_told_why(monkeypatch):
    tab = Tab(turn_age_s=95)
    check(monkeypatch, tab)
    assert tab.client.interrupts == 1
    assert len(tab.injected) == 1
    note = tab.injected[0]
    assert "NOT by the user" in note and "automatic execution control" in note
    assert "95s the turn produced no reply and no tool call" in note


def test_a_running_tool_call_is_not_an_empty_turn(monkeypatch):
    tab = Tab(turn_age_s=95)
    tab._track_tool_outcomes(AssistantMessage(
        content=[ToolUseBlock(id="t1", name="PowerShell", input={"command": "python get_answer.py"})], model="m",
    ))
    check(monkeypatch, tab)
    assert tab.client.interrupts == 0 and tab.injected == []


def test_a_finished_tool_call_no_longer_counts(monkeypatch):
    tab = Tab(turn_age_s=95)
    tab._track_tool_outcomes(AssistantMessage(content=[ToolUseBlock(id="t1", name="PowerShell", input={})], model="m"))
    tab._track_tool_outcomes(UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")]))
    assert tab._open_tool_calls == {}


def test_a_message_joining_a_running_turn_is_not_an_empty_turn(monkeypatch):
    tab = Tab(turn_age_s=95)
    tab.turn_joined_running = True
    check(monkeypatch, tab)
    assert tab.client.interrupts == 0 and tab.injected == []


def test_the_reason_names_the_stopped_tool_call(monkeypatch):
    tab = Tab(turn_age_s=16 * 60)  # past the absolute ceiling, which still applies with a tool running
    tab._open_tool_calls["t1"] = ("PowerShell", time.monotonic() - 600)
    check(monkeypatch, tab)
    note = tab.injected[0]
    assert "past the 900s limit for one turn" in note
    assert "'PowerShell' had been running 600s and was stopped" in note

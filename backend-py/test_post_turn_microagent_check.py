"""The post-turn completion check is decided by the microagent only (chat_session_mixins/watchdog.py).
Run: python -m pytest test_post_turn_microagent_check.py -q"""

import asyncio
from types import SimpleNamespace

from app import small_model_engine as sme
from app.chat_session_mixins.watchdog import WatchdogMixin


class Tab(WatchdogMixin):
    def __init__(self):
        self.tab_id, self.workspace_dir, self.ended = "t", "ws", False
        self._post_turn_checks_halted = False
        self._awaiting_post_turn_check_reply = False
        self.injected = []

    def inject_proactive(self, text, *a, **k):
        self.injected.append(text)
        return True

    def _ladder_recent_dialogue(self):
        return ""


def verdict(monkeypatch, answer):
    async def fake(*args):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(sme, "verify_claude_attempt", fake)


def run(tab):
    asyncio.run(tab._post_turn_microagent_verdict("NUDGE", "task", "reply", ["- Bash -> ok"]))


def test_finished_work_sends_nothing_to_the_model(monkeypatch):
    tab = Tab()
    verdict(monkeypatch, ("done", ""))
    run(tab)
    assert tab.injected == [] and not tab._awaiting_post_turn_check_reply


def test_unfinished_work_reaches_the_model_with_the_reason(monkeypatch):
    tab = Tab()
    verdict(monkeypatch, ("up", "the second letter was not sent"))
    run(tab)
    assert len(tab.injected) == 1 and tab.injected[0].startswith("NUDGE") and "second letter" in tab.injected[0]
    assert tab._awaiting_post_turn_check_reply


def test_a_failing_microagent_sends_nothing(monkeypatch):
    tab = Tab()
    verdict(monkeypatch, RuntimeError("down"))
    run(tab)
    assert tab.injected == []

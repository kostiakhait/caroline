"""A ResultMessage ends the pending turn only when the CLI has started that turn's input
(chat_session_mixins/engine_lifecycle.py, _result_precedes_pending_input / _input_stream).
Run: python -m pytest test_stale_result.py -q"""

import asyncio

from app.chat_session_mixins.engine_lifecycle import EngineLifecycleMixin


class Tab(EngineLifecycleMixin):
    def __init__(self, engine_kind="claude"):
        self.tab_id, self.engine_kind, self.ended = "stale-test", engine_kind, False
        self.queue = []
        self._queue_event = asyncio.Event()
        self._cli_turn_running = False
        self._input_preparing = False
        self._input_awaiting_init = False
        self.turn_is_voice = False

    def _rung_advice_begin(self, item): pass
    def _glue_context_recovery(self, item): pass
    def _dr_begin(self, item): pass
    def _inflight_begin(self): pass

    async def _ladder_before_yield(self, item):
        # the advisors' wait: here the input is off the queue but not with the CLI
        assert self._input_preparing


def first_yield(tab):
    async def go():
        stream = tab._input_stream()
        message = await stream.__anext__()
        await stream.aclose()
        return message
    return asyncio.run(go())


def test_a_result_with_no_pending_input_ends_the_turn():
    assert not Tab()._result_precedes_pending_input()


def test_a_result_while_input_is_still_queued_is_stale():
    tab = Tab()
    tab.queue.append({"message": "next", "is_voice": False})
    assert tab._result_precedes_pending_input()


def test_a_result_while_input_is_being_prepared_is_stale():
    tab = Tab()
    tab._input_preparing = True
    assert tab._result_precedes_pending_input()


def test_input_handed_to_an_idle_cli_waits_for_its_init():
    tab = Tab()
    tab.queue.append({"message": "hello", "is_voice": False})
    assert first_yield(tab) == "hello"
    assert not tab._input_preparing and tab._input_awaiting_init
    assert tab._result_precedes_pending_input()   # the 23:20:54 case: the old turn's result, read late


def test_input_handed_to_a_busy_cli_does_not_wait():
    tab = Tab()
    tab._cli_turn_running = True                   # merged into that turn or queued behind it: undecidable
    tab.queue.append({"message": "hello", "is_voice": False})
    first_yield(tab)
    assert not tab._input_awaiting_init and not tab._result_precedes_pending_input()


def test_codex_is_never_stale():
    tab = Tab(engine_kind="openai")
    tab._input_awaiting_init = True
    assert not tab._result_precedes_pending_input()

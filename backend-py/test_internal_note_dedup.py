"""An internal note already waiting in the queue is not queued a second time (chat_session_mixins/dialogue_transcript.py).
Run: python -m pytest test_internal_note_dedup.py -q"""

import asyncio

from app.chat_session_mixins.dialogue_transcript import DialogueTranscriptMixin as Mixin


class Tab(Mixin):
    def __init__(self):
        self.tab_id, self.queue = "t", []
        self._queue_event = asyncio.Event()
        self._stop_note_in_next_push = False


def test_identical_internal_note_is_queued_once():
    tab = Tab()
    for _ in range(3):
        tab._push_message("Check whether you actually finished", [], False)
    assert len(tab.queue) == 1


def test_different_notes_and_real_messages_are_kept():
    tab = Tab()
    tab._push_message("note A", [], False)
    tab._push_message("note B", [], False)
    tab._push_message("hello", [], False, memory_advice_for="hello")
    tab._push_message("hello", [], False, memory_advice_for="hello")
    assert len(tab.queue) == 4

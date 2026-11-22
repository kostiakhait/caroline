"""A recovery replay carries Caroline's service note and the user's own words in one message: the transcript keeps
only the user's words (history.py, _is_internal_block). Run: python -m pytest test_internal_note_history.py -q"""

import json

from app.history import SYNTHETIC_TURN_MARKER, _extract_entries_from_jsonl


def user_line(blocks):
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": t} for t in blocks]}})


def test_replayed_user_text_survives_the_service_note():
    raw = user_line(["[Sent: Mon 10:00]", SYNTHETIC_TURN_MARKER + "[System note: recovered from an internal failure]", "hello there"])
    entries = _extract_entries_from_jsonl(raw, "t")
    assert len(entries) == 1
    assert "hello there" in entries[0]["text"] and "System note" not in entries[0]["text"]


def test_a_note_alone_is_dropped():
    raw = user_line(["[Sent: Mon 10:00]", SYNTHETIC_TURN_MARKER + "[System note: recovered]"])
    assert _extract_entries_from_jsonl(raw, "t") == []

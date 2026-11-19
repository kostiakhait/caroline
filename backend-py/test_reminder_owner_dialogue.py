"""A user reminder's fresh session gets the owner's last-24h dialogue (app/main.py _owner_dialogue_for_reminder),
and the reply agents reject a reply that repeats their output-format instructions (app/microagents/format_echo.py).
Run: python -m pytest test_reminder_owner_dialogue.py -q"""

import json
import time

from app import dialogue_log, main
from app.microagents import reply_review, reply_translate

SCHEMA_TEXT = json.dumps({"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})


def entry(age_s, role, text, kind=dialogue_log.SHOWN):
    ms = int((time.time() - age_s) * 1000)
    return {"ms": ms, "ts": "t", "role": role, "kind": kind, "text": text}


def test_reminder_gets_the_owners_decisions(monkeypatch):
    entries = [
        entry(30 * 3600, "user", "too old"),
        entry(6 * 3600, "user", "По Motion to Dismiss ничего не делаем"),
        entry(4 * 3600, "user", "Неинтересно, проехали"),
        entry(4 * 3600, "assistant", "hidden", kind=dialogue_log.WITHHELD),
    ]
    monkeypatch.setattr(dialogue_log, "read_entries", lambda ws, tab: list(entries))
    text = main._owner_dialogue_for_reminder("1")
    assert "ничего не делаем" in text and "проехали" in text
    assert "too old" not in text and "hidden" not in text
    assert "never ask again" in text


def test_reminder_without_dialogue_or_on_failure(monkeypatch):
    monkeypatch.setattr(dialogue_log, "read_entries", lambda ws, tab: [])
    assert main._owner_dialogue_for_reminder("1") == ""
    monkeypatch.setattr(dialogue_log, "read_entries", lambda ws, tab: 1 / 0)
    assert main._owner_dialogue_for_reminder("1") == ""


# The live reply of 2026-11-19: Russian text, the closing "---" and the template translated into Russian.
LEAKED = (
    "Отметила прочитанными 11 писем.\n---\n\nОтветьте одним значением JSON и ничего более. Оно должно "
    "соответствовать этой схеме JSON: " + SCHEMA_TEXT
)


def test_translate_rejects_the_translated_format_instructions():
    source = "Отметила прочитанными 11 писем."
    assert reply_translate.verify_translate({"text": source}, {"text": LEAKED})
    assert reply_translate.verify_translate({"text": source}, {"text": source}) is None


def test_translate_rejects_a_bare_closing_rule():
    assert reply_translate.verify_translate({"text": "Привет"}, {"text": "Привет\n---"})


def test_translate_keeps_rules_and_schemas_that_are_in_the_text():
    source = "Раздел\n---\nСхема: " + SCHEMA_TEXT
    assert reply_translate.verify_translate({"text": source}, {"text": source}) is None


def test_review_rejects_the_format_instructions():
    assert reply_review.verify_review({"text": "Отчёт", "task": "t"}, {"text": "Отчёт\n" + SCHEMA_TEXT})
    assert reply_review.verify_review({"text": "Отчёт", "task": "t"}, {"text": "Отчёт"}) is None

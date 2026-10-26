"""The exact dialogue record and its tool (app/dialogue_log.py, plugins/dialogue_plugin.py).
Run: python -m pytest test_dialogue_log.py -q"""

import asyncio
import json
import time

import pytest

from app import dialogue_log as dl
from app.chat_session_mixins.delivery_report import DeliveryReportMixin
from app.plugins import dialogue_plugin
from app.plugins.loader import discover_plugins


def assistant_env(text, **extra):
    return {"type": "sdk_message", "message": {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}, **extra}


def test_append_read_render_in_order(tmp_path):
    ws = str(tmp_path)
    dl.append(ws, "1", dl.USER, "send it", voice=True)
    dl.append(ws, "1", dl.ASSISTANT, "Still sending", dl.NOTE)
    dl.append(ws, "1", dl.ASSISTANT, "Said but filtered", dl.WITHHELD)
    dl.append(ws, "1", dl.ASSISTANT, "Sent to Oksana.", dl.SHOWN)
    dl.append(ws, "1", dl.ASSISTANT, "   ")                      # empty: not written
    text = dl.render(dl.select(dl.read_entries(ws, "1")))
    assert text.index("send it") < text.index("Still sending") < text.index("Sent to Oksana.")
    assert "User (voice)" in text and "[progress note]" in text and "WITHHELD" in text
    assert "(no messages match)" == dl.render([])


def test_select_filters(tmp_path):
    ws = str(tmp_path)
    for i in range(5):
        dl.append(ws, "1", dl.ASSISTANT, f"message {i}", dl.SHOWN)
    dl.append(ws, "1", dl.ASSISTANT, "hidden one", dl.WITHHELD)
    entries = dl.read_entries(ws, "1")
    assert [e["text"] for e in dl.select(entries, limit=2)] == ["message 4", "hidden one"]
    assert "hidden one" not in [e["text"] for e in dl.select(entries, include_withheld=False)]
    assert [e["text"] for e in dl.select(entries, contains="MESSAGE 3")] == ["message 3"]
    assert dl.select(entries, since_minutes=1) and not dl.select([{**e, "ms": 1} for e in entries], since_minutes=1)


def test_long_output_drops_the_oldest_and_says_so(tmp_path):
    ws = str(tmp_path)
    for i in range(10):
        dl.append(ws, "1", dl.ASSISTANT, f"{i} " + "x" * 5000, dl.SHOWN)
    out = dl.render(dl.select(dl.read_entries(ws, "1"), limit=10))
    assert "oldest" in out and out.rstrip().endswith("x") and "9 xxxx" in out and "0 xxxx" not in out


def test_tabs_are_separate_and_a_bad_line_is_skipped(tmp_path):
    ws = str(tmp_path)
    dl.append(ws, "1", dl.USER, "tab one")
    dl.append(ws, "2", dl.USER, "tab two")
    with open(dl.log_path(ws, "1"), "a", encoding="utf-8") as f:
        f.write("not json\n")
    assert [e["text"] for e in dl.read_entries(ws, "1")] == ["tab one"]
    assert [e["text"] for e in dl.read_entries(ws, "2")] == ["tab two"]
    assert dl.read_entries(ws, "nope") == []


def test_tool_reads_the_current_tab(tmp_path, monkeypatch):
    monkeypatch.setattr(dialogue_plugin, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(dialogue_plugin, "get_tab_id", lambda: "7")
    dl.append(str(tmp_path), "7", dl.USER, "hello there")
    dl.append(str(tmp_path), "1", dl.USER, "another tab")
    out = asyncio.run(dialogue_plugin.get_dialogue_transcript({"limit": 5}, None))["text"]
    assert "hello there" in out and "another tab" not in out
    out = asyncio.run(dialogue_plugin.get_dialogue_transcript({"contains": "nothing like it"}, None))["text"]
    assert out == "(no messages match)"


def test_plugin_is_discovered_and_stays_directly_callable():
    found = {p.name: p for p in discover_plugins()}
    assert "dialogue" in found and found["dialogue"].always_loaded
    assert [t.name for t in found["dialogue"].tools] == ["get_dialogue_transcript"]


class Session(DeliveryReportMixin):
    def __init__(self, ws, headless=False):
        self.tab_id, self.workspace_dir, self.headless = "1", ws, headless

        async def raw(envelope):
            pass

        self.send = self._dr_wrap_send(raw)


def test_what_reaches_the_window_is_logged_with_its_kind(tmp_path):
    s = Session(str(tmp_path))

    async def go():
        await s.send(assistant_env("Sent to Oksana."))
        await s.send(assistant_env("Checking", isNarration=True))
        await s.send(assistant_env("The email was sent.", isDeliveryReport=True))
        await s.send({"type": "status", "state": "working"})
        await s.send(assistant_env("[[NO_UPDATE]]"))

    asyncio.run(go())
    got = [(e["kind"], e["text"]) for e in dl.read_entries(str(tmp_path), "1")]
    assert got == [(dl.SHOWN, "Sent to Oksana."), (dl.NOTE, "Checking"), (dl.REPORT, "The email was sent.")]


def test_a_headless_session_logs_nothing(tmp_path):
    s = Session(str(tmp_path), headless=True)
    asyncio.run(s.send(assistant_env("background chatter")))
    assert dl.read_entries(str(tmp_path), "1") == []

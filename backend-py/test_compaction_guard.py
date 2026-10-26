"""Compaction that compacts, not clears (app/compaction_guard.py, chat_session_mixins/compaction_recovery.py).
Run: python -m pytest test_compaction_guard.py -q"""

import asyncio
import json
import time

import pytest

from app import compaction_guard as cg
from app import small_model_engine as sme
from app.chat_session import ChatSession
from app.chat_session_mixins import compaction_recovery as cr

GOOD = "Summary:\n1. Primary Request and Intent:\n   - " + "The owner asked for a mail report every two hours. " * 30
DRAFT = "Hi Nick, thanks for the proposal about distribution of your routers in Russia. We asked for a price list and never got it."


def user(text, ts="2026-10-26T19:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def assistant(text, extra=None, ts="2026-10-26T19:01:00Z"):
    blocks = ([{"type": "text", "text": text}] if text else []) + (extra or [])
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": blocks}}


def boundary():
    return {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"}


def continuation(summary):
    return user(cg.CONTINUATION_PREFIX + " from a previous conversation that ran out of context.\n<tools-used-before-summary>Read</tools-used-before-summary>\n"
                + summary + "\n\nIf you need specific details from before compaction, read the full transcript at: x")


def write(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


# --- the check ---------------------------------------------------------------------------

@pytest.mark.parametrize("summary,degenerate", [
    (GOOD, False), ("[[NO_UPDATE]]", True), ("Доброе утро, Константин. Я снова на связи и готова к работе.", True),
    ("", True), (None, True), (GOOD + " The assistant answers internal messages with [[NO_UPDATE]].", False),
    ("[[NO_UPDATE]]" + GOOD, True),
])
def test_what_is_a_degenerate_summary(summary, degenerate):
    assert cg.is_degenerate(summary) is degenerate


def test_last_summary_is_read_after_the_last_boundary(tmp_path):
    f = tmp_path / "s.jsonl"
    write(f, [user("old"), boundary(), continuation(GOOD), user("later"), boundary(), continuation("[[NO_UPDATE]]")])
    summary, index, rows = cg.last_summary(f)
    assert summary == "[[NO_UPDATE]]" and index == 4 and len(rows) == 6
    assert cg.last_summary(tmp_path / "missing.jsonl") == (None, None, [])
    write(f, [user("no compaction yet")])
    assert cg.last_summary(f)[0] is None


def test_the_digest_keeps_the_real_conversation_and_the_draft_and_drops_internal_noise():
    rows = [
        user("[Sent: Mon, Oct 26, 2026]\nНапиши Нику про прайс"),
        assistant("Вот текст для Ника:\n> " + DRAFT + "\nОтправить?"),
        user(cg.CONTINUATION_PREFIX + " x"),
        user("⁣[[caroline-internal-turn]]⁣[Internal: check]"),
        assistant("[[NO_UPDATE]]"),
        assistant("", [{"type": "tool_use", "id": "1", "name": "email_send", "input": {"to": "n@x.org"}}]),
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "1", "content": "Sent " * 200}]}},
        boundary(),
    ]
    digest = cg.digest_before_boundary(rows, len(rows) - 1)
    assert "USER: Напиши Нику про прайс" in digest and DRAFT in digest and "[tool] email_send" in digest
    assert "internal" not in digest and "[[NO_UPDATE]]" not in digest and "[Sent:" not in digest
    assert len(cg.digest_before_boundary(rows, 7, max_chars=50)) <= 54


def test_the_compact_command_is_a_summarization_task():
    assert cg.COMPACT_COMMAND.startswith("/compact ")
    assert "NEVER answer with [[NO_UPDATE]]" in cg.COMPACT_COMMAND and "VERBATIM" in cg.COMPACT_COMMAND and "draft" in cg.COMPACT_COMMAND


# --- the wiring --------------------------------------------------------------------------

@pytest.fixture
def session(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()

    async def nothing(*a, **k):
        return None

    s = ChatSession(tab_id="2", workspace_dir=str(ws), send=nothing)
    s.last_saved_session_id = "sess"
    monkeypatch.setattr(cr, "claude_project_dir", lambda workspace: projects)
    monkeypatch.setattr(cr, "SUMMARY_CHECK_DELAY_S", 0)
    return s, projects


def test_a_cleared_context_is_rebuilt_and_handed_over_with_the_next_message(session, monkeypatch):
    s, projects = session
    write(projects / "sess.jsonl", [
        user("Напиши Нику про прайс"), assistant("Вот текст для Ника:\n> " + DRAFT), boundary(), continuation("[[NO_UPDATE]]"),
    ])
    seen = {}

    async def fake_summarize(digest, tab_id):
        seen["digest"] = digest
        return "REBUILT: a draft for Nick awaits approval."

    monkeypatch.setattr(sme, "summarize_conversation", fake_summarize)
    run(s._check_compaction_summary())
    assert DRAFT in seen["digest"]
    assert "REBUILT: a draft for Nick awaits approval." in s._context_recovery and "Context recovery" in s._context_recovery
    item = {"message": {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Отсылай"}]}}}
    s._glue_context_recovery(item)
    assert len(item["message"]["message"]["content"]) == 2 and "REBUILT" in item["message"]["message"]["content"][1]["text"]
    assert s._context_recovery is None


def test_the_rebuild_falls_back_to_the_digest_when_the_summarizer_fails(session, monkeypatch):
    s, projects = session
    write(projects / "sess.jsonl", [user("Напиши Нику"), assistant("Черновик: " + DRAFT), boundary(), continuation("[[NO_UPDATE]]")])

    async def failing(digest, tab_id):
        return None

    monkeypatch.setattr(sme, "summarize_conversation", failing)
    run(s._check_compaction_summary())
    assert DRAFT in s._context_recovery


def test_a_good_summary_is_left_alone(session, monkeypatch):
    s, projects = session
    write(projects / "sess.jsonl", [user("old"), boundary(), continuation(GOOD)])

    async def must_not_run(*a, **k):
        raise AssertionError("no rebuild for a good summary")

    monkeypatch.setattr(sme, "summarize_conversation", must_not_run)
    run(s._check_compaction_summary())
    assert s._context_recovery is None


def test_a_command_does_not_carry_the_recovery(session):
    s, _ = session
    s._context_recovery = "[Context recovery ...]"
    command = {"message": {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "/compact x"}]}}}
    s._glue_context_recovery(command)
    assert len(command["message"]["message"]["content"]) == 1 and s._context_recovery


def test_the_state_block_holds_open_actions_done_actions_and_the_exact_last_messages(session):
    from app import action_ledger, dialogue_log

    s, _ = session
    s.action_registry.open_new("Send the draft to Nick Jiang about the price list")
    action_ledger.append(s.workspace_dir, "2", "email_send", {"to": ["oksana@x.org"], "subject": "Re: Misha"})
    dialogue_log.append(s.workspace_dir, "2", dialogue_log.USER, "Нику напиши про прайс")
    dialogue_log.append(s.workspace_dir, "2", dialogue_log.ASSISTANT, "Вот текст: " + DRAFT)
    state = s._compaction_state_text()
    assert "OPEN ACTIONS" in state and "Nick Jiang" in state and "ALREADY SENT" in state and DRAFT in state


class FakeClient:
    def __init__(self):
        self.models = []

    async def set_model(self, model):
        self.models.append(model)


def test_the_compaction_runs_on_a_strong_model_with_the_instruction(session, monkeypatch):
    s, _ = session
    s.client, s.engine_kind = FakeClient(), "claude"
    monkeypatch.setattr("app.chat_session_mixins.ladder.get_model_override", lambda ws, p: None)
    pushed = []
    s._push_internal_command = pushed.append
    run(s._run_forced_compaction())
    assert s.client.models == ["sonnet"] and pushed == [cg.COMPACT_COMMAND]


def test_a_model_pinned_in_settings_is_not_switched_for_the_compaction(session, monkeypatch):
    s, _ = session
    s.client, s.engine_kind = FakeClient(), "claude"
    monkeypatch.setattr("app.chat_session_mixins.ladder.get_model_override", lambda ws, p: "opus")
    pushed = []
    s._push_internal_command = pushed.append
    run(s._run_forced_compaction())
    assert s.client.models == [] and pushed == [cg.COMPACT_COMMAND]


# --- when a compaction may start ---------------------------------------------------------

def _ready(s, monkeypatch):
    s.engine_kind, s.turn_pending, s.forced_compaction_result_pending = "claude", False, False
    s.conn_state = {"kind": "connected"}
    s.forced_compaction_blocked_until = 0
    started = []

    async def fake_run():
        started.append(1)

    async def fake_publish():
        return None

    monkeypatch.setattr(s, "_run_forced_compaction", fake_run)
    monkeypatch.setattr(s, "_publish_status", fake_publish)
    return started


def _check(s):
    async def go():
        s._check_forced_compaction()
        await asyncio.sleep(0.01)

    run(go())


def test_no_compaction_at_the_start_of_a_session(session, monkeypatch):
    s, _ = session
    started = _ready(s, monkeypatch)
    s.needs_startup_compaction = True
    s.last_known_context_tokens = 500_000
    s.last_forced_compaction_at = None
    _check(s)
    assert not started and s.needs_startup_compaction is False and s.last_forced_compaction_at is not None


def test_no_compaction_of_a_small_context_even_when_the_hour_is_up(session, monkeypatch):
    s, _ = session
    started = _ready(s, monkeypatch)
    s.last_forced_compaction_at = time.monotonic() - 2 * 3600
    s.last_known_context_tokens = 50_748            # the context that was compacted into nothing on 2026-10-26
    _check(s)
    assert not started


def test_a_large_context_is_compacted_when_the_hour_is_up(session, monkeypatch):
    s, _ = session
    started = _ready(s, monkeypatch)
    s.last_forced_compaction_at = time.monotonic() - 2 * 3600
    s.last_known_context_tokens = 200_000
    _check(s)
    assert started

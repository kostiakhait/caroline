"""The action log kept by code, the threaded email reply, and the prompts of the checks that fought each other
(2026-10-26). Run: python -m pytest test_action_ledger_and_reply.py -q"""

import asyncio

import pytest

from app import action_ledger as al
from app import operations
from app import small_model_engine as sme
from app.microagents import delivery_completeness, question_already_answered
from app.plugins import email_plugin


def run(coro):
    return asyncio.run(coro)


# --- the action log -----------------------------------------------------------------

def test_describe_and_fact_text_for_an_email():
    entry = al.describe("email_send", {"address": "me@x.org", "to": ["oksana@x.org"], "subject": "Re: Misha", "text": "Hello " * 200})
    assert entry["to"] == "oksana@x.org" and entry["from"] == "me@x.org" and len(entry["text"]) == al.MAX_TEXT_CHARS
    fact = al.fact_text({**entry, "when": "2026-10-26T08:52:08-04:00"})
    assert "DONE on 2026-10-26" in fact and "oksana@x.org" in fact and "not a draft" in fact and "Re: Misha" in fact


def test_append_recent_and_flush_addendum(tmp_path):
    ws = str(tmp_path)
    assert al.flush_addendum(ws) == ""
    al.append(ws, "1", "email_send", {"to": ["a@x.org"], "subject": "Hi", "text": "t"})
    al.append(ws, "1", "telegram_send_message", {"chat": "Bob", "text": "ok"})
    assert [e["tool"] for e in al.recent(ws)] == ["email_send", "telegram_send_message"]
    note = al.flush_addendum(ws)
    assert "SENT" in note and "a@x.org" in note and "Never save them as unsent" in note
    assert al.recent(ws, hours=0.0000001) == []
    (tmp_path / "action-ledger.jsonl").write_text("broken\n", encoding="utf-8")
    assert al.recent(ws) == []


def test_a_finished_send_is_recorded_and_saved_by_code(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "WORKSPACE_DIR", str(tmp_path))
    saved = []

    async def fake_save(text, tab_id):
        saved.append((text, tab_id))
        return True

    monkeypatch.setattr("app.plugins.memory_plugin.save_fact_from_code", fake_save)

    async def handler(args, report_progress):
        return {"text": "Sent"}

    async def go():
        env = await operations.dispatch("email", "email_send", handler, {"to": ["oksana@x.org"], "subject": "Hi", "text": "Body"})
        await asyncio.sleep(0.05)
        return env

    env = run(go())
    assert env["status"] == "done"
    assert [e["to"] for e in al.recent(str(tmp_path))] == ["oksana@x.org"]
    assert saved and "oksana@x.org" in saved[0][0]


def test_a_failed_send_is_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "WORKSPACE_DIR", str(tmp_path))

    async def handler(args, report_progress):
        raise RuntimeError("smtp down")

    run(operations.dispatch("email", "email_send", handler, {"to": ["a@x.org"], "subject": "Hi"}))
    assert al.recent(str(tmp_path)) == []


# --- the threaded reply -------------------------------------------------------------

ORIGINAL = {"subject": "Миша, терапия", "from": "Оксана <khait.oksan@gmail.com>", "to": "me@x.org, other@x.org",
            "cc": "third@x.org", "messageId": "<abc@mail>", "references": ["<root@mail>"]}


def test_reply_fields_take_subject_addressee_and_thread_from_the_original():
    f = email_plugin.reply_fields(ORIGINAL, "me@x.org", reply_all=False)
    assert f["to"] == ["khait.oksan@gmail.com"] and f["subject"] == "Re: Миша, терапия"
    assert f["inReplyTo"] == "<abc@mail>" and f["references"] == ["<root@mail>", "<abc@mail>"] and "cc" not in f
    f = email_plugin.reply_fields({**ORIGINAL, "subject": "Re: already"}, "me@x.org", reply_all=True)
    assert f["subject"] == "Re: already" and sorted(f["cc"]) == ["other@x.org", "third@x.org"]
    assert email_plugin.reply_fields({"subject": "x", "from": "a@x.org"}, "", False).get("inReplyTo") is None


def test_email_send_refuses_a_bare_re_subject(monkeypatch):
    async def fill(args):
        return args

    monkeypatch.setattr(email_plugin.email_credentials, "fill", fill)
    with pytest.raises(ValueError, match="email_send_reply"):
        run(email_plugin.email_send({"to": ["a@x.org"], "subject": "Re: Обучение Матвея", "text": "x"}, None))


def test_email_send_reply_reads_the_original_then_sends_in_its_thread(monkeypatch):
    sent = []

    async def fake_fetch(args, rp):
        assert args["folder"] == "INBOX" and args["uid"] == 7
        return dict(ORIGINAL)

    async def fake_send(args, rp):
        sent.append(args)
        return {"text": "Sent"}

    monkeypatch.setattr(email_plugin, "_fetch_original", fake_fetch)
    monkeypatch.setattr(email_plugin, "email_send", fake_send)
    call = {"address": "me@x.org", "folder": "INBOX", "uid": 7, "text": "Answer"}
    result = run(email_plugin.email_send_reply(call, None))
    assert result == {"text": "Sent"}
    assert sent[0]["to"] == ["khait.oksan@gmail.com"] and sent[0]["subject"] == "Re: Миша, терапия"
    assert sent[0]["inReplyTo"] == "<abc@mail>" and sent[0]["_reply_ok"] and "uid" not in sent[0]
    assert call["to"] == ["khait.oksan@gmail.com"] and call["subject"] == "Re: Миша, терапия"   # what dispatch() records
    with pytest.raises(ValueError):
        run(email_plugin.email_send_reply({"address": "me@x.org", "text": "x"}, None))


def test_the_reply_tool_is_registered_as_a_guarded_send():
    assert "email_send_reply" in [t.name for t in email_plugin.PLUGIN.tools]
    assert "send" in "email_send_reply" and operations.REPLY_WATCH_CHANNEL_BY_TOOL["email_send_reply"] == "email"


# --- the checks that fought each other ----------------------------------------------

def test_prompts_let_status_and_approval_messages_through():
    d = delivery_completeness.prompt_completeness({"final_text": "x", "recent_dialogue": "y"}, None)
    assert "STATUS" in d and "complete, even when a draft is being discussed" in d
    q = question_already_answered.prompt_already_answered({"final_text": "x", "recent_dialogue": "y"}, None)
    assert "request for APPROVAL" in q and "already_answered=false" in q


def test_ladder_verifier_sees_the_recent_dialogue_and_earlier_work():
    tracker = sme._TurnTracker()
    dialogue = "[t] User:\nsend it\n\n[t] Caroline:\nSent to Oksana."
    prompt = sme._build_verification_prompt(tracker, "I already sent it.", True, [], claude_attempt=True, recent_dialogue=dialogue)
    assert "RECENT DIALOGUE" in prompt and "Sent to Oksana." in prompt and "unless the RECENT DIALOGUE" in prompt
    assert "RECENT DIALOGUE" not in sme._build_verification_prompt(tracker, "x", True, [], claude_attempt=True)


def test_delivery_correction_has_a_cooldown():
    from app.chat_session_mixins import dialogue_transcript as dt

    assert dt.DELIVERY_CORRECTION_COOLDOWN_S >= 60


def test_checker_note_marks_sent_actions_for_the_delivery_check(tmp_path):
    ws = str(tmp_path)
    assert al.checker_note(ws) == ""
    al.append(ws, "1", "email_send", {"to": ["oksana@x.org"], "subject": "Re: Misha"})
    note = al.checker_note(ws)
    assert "ALREADY SENT" in note and "oksana@x.org" in note and "never lacks a 'draft to send'" in note

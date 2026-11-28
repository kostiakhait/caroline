"""The faults seen in one tab's transcript on 2026-11-27: safety refusals, slow replies, repeated replies, the context
size, the record of the recent dialogue, and the watch for an answer to a letter.
Run: python -m pytest test_tab_transcript_fixes.py -q"""

import asyncio

import pytest

from app import chat_session_shared as shared
from app import dialogue_log
from app.chat_session import ChatSession
from app.failure_classification import extract_classifier_refusal_category, is_classifier_refusal
from app.language_hint import already_in_language
from app.plugins import voice_api

REFUSAL = (
    "API Error: Haiku 5.5's safeguards flagged this message. Safety measures flag messages on topics like cybersecurity "
    "or biology. Details: `[cyber]`"
)


def run(coro):
    return asyncio.run(coro)


class Client:
    def __init__(self, fails=False):
        self.models, self.fails = [], fails

    async def set_model(self, model):
        if self.fails:
            raise RuntimeError("no")
        self.models.append(model)


@pytest.fixture
def session(tmp_path, monkeypatch):
    sent = []

    async def send(envelope):
        sent.append(envelope)

    s = ChatSession(tab_id="2", workspace_dir=str(tmp_path), send=send)
    s.sent, s.notes = sent, []
    s.engine_kind = "claude"
    monkeypatch.setattr(s, "inject_proactive", lambda text, **kw: s.notes.append(text) or True)
    return s


# --- safety refusals ------------------------------------------------------------

def test_the_current_refusal_wording_is_recognized():
    assert is_classifier_refusal(REFUSAL, "<synthetic>", "refusal")
    assert is_classifier_refusal(REFUSAL, None, None)
    assert extract_classifier_refusal_category(REFUSAL) == "cyber"
    assert not is_classifier_refusal("В логе было: " + REFUSAL, "claude-sonnet-5-5", "end_turn")


def test_a_refused_turn_goes_to_the_next_model(session):
    session.client = Client()
    session._ladder_model = "haiku"
    session._refusal_pending = "cyber"
    assert run(session._refusal_on_result()) is True
    assert session.client.models == ["sonnet"] and session._refusal_floor == 1
    assert session.queue[0]["ladder_continuation"] and session._ladder.model == "sonnet"
    session._refusal_pending = "cyber"
    assert run(session._refusal_on_result()) is True
    assert session.client.models == ["sonnet", "opus"]
    assert not session._refusal_can_move_up()


def test_a_model_switch_that_fails_still_tells_the_user(session, monkeypatch):
    async def same(wire):
        return wire

    monkeypatch.setattr(session, "_translate_wire_visible_text", same)
    session.client = Client(fails=True)
    session._ladder_model = "haiku"
    session._refusal_pending = "cyber"
    assert run(session._refusal_on_result()) is False
    text = session.sent[0]["message"]["message"]["content"][0]["text"]
    assert text == shared.SAFETY_FILTER_EXPLANATION and "API Error" not in text


def test_a_result_without_a_refusal_is_left_alone(session):
    assert run(session._refusal_on_result()) is False and session.sent == []


# --- speed and repeats ----------------------------------------------------------

def test_a_russian_reply_is_not_translated_again():
    assert already_in_language("Письмо для Oksana готово, тема: Michelle.", "Russian")
    assert not already_in_language("The letter is ready to go.", "Russian")
    assert not already_in_language("Готово. I will send it tomorrow morning.", "Russian")
    assert not already_in_language("Лист готовий, відправити його?", "Russian")
    assert not already_in_language("Письмо готово.", "English")


def test_a_message_without_a_question_or_a_waiting_draft_is_not_checked(session, monkeypatch):
    async def never(final_text, dialogue):
        raise AssertionError("must not be called")

    monkeypatch.setattr(voice_api, "check_question_already_answered", never)
    assert run(session._check_question_already_answered("Готово, письмо отправлено.")) is None
    assert run(session._check_question_already_answered("> Hello\n\nОтправить так?")) is None


def test_a_late_memory_block_joins_only_the_turn_it_was_looked_up_for(session):
    async def deliver(question, pending):
        task = asyncio.get_running_loop().create_future()
        task.set_result("MEMORY BLOCK")
        session.turn_pending, session.last_real_user_question = pending, "q"
        session._deliver_late_memory(task, question)

    run(deliver("q", False))
    run(deliver("older", True))
    assert session.notes == []
    run(deliver("q", True))
    assert len(session.notes) == 1 and "MEMORY BLOCK" in session.notes[0] and "[[NO_UPDATE]]" in session.notes[0]


def test_a_stale_state_check_is_not_replayed_beside_a_new_message(session, monkeypatch):
    replayed = []
    monkeypatch.setattr(session, "submit", lambda text, *a, **kw: replayed.append(text))
    check = shared.CONTINUE_OR_SILENT_NUDGE_TEMPLATE
    item = lambda text, real: {"text": text, "attachments": [], "is_real_user": real, "is_voice": False}  # noqa: E731
    session.compaction_queued_turns = [item(check, False), item("новое поручение", True)]
    session._drain_compaction_queue()
    assert replayed[0] == "новое поручение" and check not in replayed
    replayed.clear()
    session.compaction_queued_turns = [item(check, False)]
    session._drain_compaction_queue()
    assert replayed == [check]


# --- the context size and the record of the dialogue ----------------------------

def test_the_context_size_is_that_of_one_call():
    usage = {"input_tokens": 3, "cache_read_input_tokens": 70000, "cache_creation_input_tokens": 500, "output_tokens": 900}
    assert shared.context_tokens_of(usage) == 70503
    assert shared.context_tokens_of(None) == 0


def test_the_recent_dialogue_is_what_the_window_showed(tmp_path):
    workspace = str(tmp_path)
    dialogue_log.append(workspace, "1", dialogue_log.USER, "Отправь письмо")
    dialogue_log.append(workspace, "1", dialogue_log.ASSISTANT, "Черновик, который не показан", dialogue_log.WITHHELD)
    dialogue_log.append(workspace, "1", dialogue_log.ASSISTANT, "Вот черновик")
    assert shared._shown_dialogue_lines("1", workspace, 0) == ["User: Отправь письмо", "Caroline: Вот черновик"]


def test_remarks_put_before_a_message_are_not_the_users_words():
    text = shared._INTERIM_NOTES_HEADER + "since their last message: \"ищу\"]\n\nОтправь письмо"
    assert shared._INTERIM_NOTES_BLOCK_PATTERN.sub("", text) == "Отправь письмо"


# --- the watch for an answer ----------------------------------------------------

def test_a_send_is_watched_and_the_answer_is_reported(session, monkeypatch):
    uid = {"value": 5}

    async def creds(address, args=None):
        return {"address": address, "password": "secret", "imapHost": "imap.x.org"}

    async def latest(_creds, person):
        return uid["value"]

    monkeypatch.setattr(session, "_reply_watch_credentials", creds)
    monkeypatch.setattr(session, "_reply_watch_latest_uid", latest)
    key = "me@x.org|oksana@y.org"

    async def scenario():
        # What a model's send looks like: the mailbox's address, no password.
        await session._arm_reply_watch({"address": "me@x.org", "to": "Oksana <oksana@y.org>", "subject": "s", "text": "t"})
        assert session._reply_watches()[key]["baseline_uid"] == 5
        assert "secret" not in session._reply_watch_file().read_text(encoding="utf-8")
        await session._reply_watch_tick(key)
        assert session.notes == []
        for timer in session._reply_watch_timers().values():
            timer.cancel()
        session._reply_watch_map, session._reply_watch_timer_map = {}, {}
        session._restore_reply_watches()                       # as after a restart of the app
        assert key in session._reply_watches()
        uid["value"] = 6
        await session._reply_watch_tick(key)
        # The correspondence goes on (2026-11-27: Oksana's second letter, 40 minutes after her first, was never reported):
        # the watch stays, its baseline is the letter just reported, and the next letter is reported too.
        assert session._reply_watches()[key]["baseline_uid"] == 6
        await session._reply_watch_tick(key)
        assert len(session.notes) == 1
        uid["value"] = 7
        await session._reply_watch_tick(key)
        for timer in session._reply_watch_timers().values():
            timer.cancel()

    run(scenario())
    assert len(session.notes) == 2 and all("oksana@y.org" in n and "email_get_thread" in n for n in session.notes)
    assert session._reply_watches()[key]["baseline_uid"] == 7


# --- drafts with blanks, and a bare go-ahead ------------------------------------

def test_a_draft_with_a_blank_is_neither_finished_nor_sendable():
    from app import claude_ladder, own_identity_gate as gate

    reply = "Вот письмо:\n\n> Оксана, здравствуйте.\n> Мне больше подходит [вариант].\n\nОтправляю после вашего «да»."
    assert "[вариант]" in claude_ladder.blanks_note(reply)
    assert "blanks" in gate.missing_reason("Мне больше подходит [вариант].", None, [], True)
    clean = "> Оксана, здравствуйте. Сайт: [здесь](https://x.org), пункт [1].\n\nОтправить [да/нет]?"
    assert claude_ladder.blanks_note(clean) == ""


def test_the_router_is_given_the_owners_own_latest_messages(tmp_path):
    from app import memory_advisor
    from app.microagents.track_route import prompt_track_route

    workspace = str(tmp_path)
    dialogue_log.append(workspace, "1", dialogue_log.USER, "Отправь Линет письмо с видео")
    for _ in range(10):
        dialogue_log.append(workspace, "1", dialogue_log.ASSISTANT, "Лена прислала сценарии")
    own = memory_advisor._owner_messages(workspace, "1")
    assert len(own) == 1 and "Линет" in own[0]
    prompt = prompt_track_route({"message": "Делаем", "tracks": [], "dialogue": [], "owner_messages": own}, None)
    assert "Отправь Линет письмо" in prompt and "bare go-ahead" in prompt

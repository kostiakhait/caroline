"""The guard against a repeated, already answered question (2026-10-30): a finished report is never hidden, and a
real repeat costs the user only the question itself. Run: python -m pytest test_question_already_answered.py -q"""

import asyncio

import pytest

from app.chat_session import ChatSession
from app.microagents.question_already_answered import unconfirmed_reason
from app.plugins import voice_api


def run(coro):
    return asyncio.run(coro)


DIALOGUE = (
    "User: Какой аккаунт Facebook подключить?\n"
    "Caroline: Ваш или мой?\n"
    "User: для моего\n"
    "User: Проверь исходящую почту khait@navlink.net - что там подавалось"
)

# The 2026-10-30 report that was hidden whole: no question in it at all.
REPORT = (
    "Константин, в исходящих khait@navlink.net за период с 1 июня я нашла 100 писем. По грантам и заявкам там "
    "три отправки.\n\n- **Access Now**, 1 августа, на grants@accessnow.org.\n\n"
    "Пункт про IRI стоит проверить первым. Сейчас я ничего никому не отправляла и ничего не меняла."
)


def test_a_report_without_a_question_is_never_a_match():
    claimed = {"already_answered": True, "answer": "Вы уже просили проверить исходящие письма.",
               "question": "Пункт про IRI стоит проверить первым.",
               "user_answer": "Проверь исходящую почту khait@navlink.net"}
    assert unconfirmed_reason(claimed, REPORT, DIALOGUE) == "no question quoted"


def test_quotes_must_really_be_there():
    text = "Подключаю Facebook. Какой аккаунт подключить:   ваш или мой?"
    ok = {"question": "Какой аккаунт подключить: ваш или мой?", "user_answer": "для моего"}
    assert unconfirmed_reason(ok, text, DIALOGUE) is None
    assert unconfirmed_reason({**ok, "question": "Какой ящик открыть?"}, text, DIALOGUE) == "quoted question is not in the message"
    assert unconfirmed_reason({**ok, "user_answer": "для вашего"}, text, DIALOGUE) == "quoted answer is not in the dialogue"
    assert unconfirmed_reason({**ok, "user_answer": ""}, text, DIALOGUE) == "quoted answer is not in the dialogue"


def test_an_unconfirmed_match_is_turned_into_no_match(monkeypatch):
    async def fake_run(name, module, agent_input):
        return {"status": "ok", "output": {"already_answered": True, "answer": "Вы уже просили проверить почту.",
                                           "question": "", "user_answer": ""}}

    monkeypatch.setattr(voice_api, "_run_local_microagent", fake_run)
    result = run(voice_api.check_question_already_answered(REPORT, DIALOGUE))
    assert result == {"already_answered": False, "answer": "", "question": ""}


@pytest.fixture
def session(tmp_path, monkeypatch):
    async def send(envelope):
        return None

    s = ChatSession(tab_id="2", workspace_dir=str(tmp_path), send=send)
    s.notes = []
    monkeypatch.setattr(s, "inject_proactive", lambda text, **kw: s.notes.append(text) or True)
    monkeypatch.setattr(s, "_gather_recent_dialogue_for_permission_guard", lambda: DIALOGUE)
    return s


def _match(question):
    async def fake_check(final_text, dialogue):
        return {"already_answered": True, "answer": "Пользователь выбрал свой аккаунт.", "question": question}

    return fake_check


def test_only_the_question_is_cut_from_a_longer_message(session, monkeypatch):
    question = "Какой аккаунт подключить: ваш или мой?"
    monkeypatch.setattr(voice_api, "check_question_already_answered", _match(question))
    text = REPORT + "\n\n" + question
    remainder = run(session._check_question_already_answered(text))
    assert remainder == REPORT
    assert "rest of the message reached the user" in session.notes[0]


def test_a_message_that_is_only_the_question_is_dropped(session, monkeypatch):
    question = "Какой аккаунт подключить: ваш или мой?"
    monkeypatch.setattr(voice_api, "check_question_already_answered", _match(question))
    assert run(session._check_question_already_answered("Уточню. " + question)) == ""
    assert "the user never saw it" in session.notes[0]


def test_no_match_leaves_the_message_alone(session, monkeypatch):
    async def fake_check(final_text, dialogue):
        return {"already_answered": False, "answer": "", "question": ""}

    monkeypatch.setattr(voice_api, "check_question_already_answered", fake_check)
    assert run(session._check_question_already_answered(REPORT)) is None
    assert session.notes == []


def test_the_wire_carries_the_rest_of_the_message(session, monkeypatch):
    async def same(text, *args, **kwargs):
        return text

    async def nothing(*args, **kwargs):
        return None

    question = "Какой аккаунт подключить: ваш или мой?"
    monkeypatch.setattr(voice_api, "review_reply", same)
    monkeypatch.setattr(voice_api, "translate_text", same)
    monkeypatch.setattr(voice_api, "check_question_already_answered", _match(question))
    for name in ("_check_permission_guard", "_check_delivery_completeness", "_patch_cli_transcript_text"):
        monkeypatch.setattr(session, name, nothing)
    monkeypatch.setattr(session, "_narration_task", lambda: "task")
    wire = {"type": "assistant", "message": {"content": [{"type": "text", "text": REPORT + "\n\n" + question}]}}
    result = run(session._translate_wire_visible_text(wire))
    assert result is wire and wire["message"]["content"][0]["text"] == REPORT

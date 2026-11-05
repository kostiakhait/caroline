"""The progress narrator must see the task's actions in the order they happened, without bookkeeping calls."""
from types import SimpleNamespace

from app.chat_session_mixins.narration_text import TextNarrationMixin, _is_bookkeeping_call


def _facts(log, pending=None, said=""):
    stub = SimpleNamespace(
        small_model_active=False,
        turn_action_log=log,
        _pending_tool_calls=pending or {},
        tab_id=987654,
        _turn_last_visible_text=said,
    )
    return TextNarrationMixin._narration_action_facts(stub)


def test_bookkeeping_names():
    assert _is_bookkeeping_call("check_operation_status")
    assert _is_bookkeeping_call("mcp__caroline-tools__describe_tool")
    assert not _is_bookkeeping_call("browser_click")


def test_chronological_and_bookkeeping_excluded():
    log = [
        ("browser_open", "url=a", False, "ok"),
        ("check_operation_status", "id=1", False, "timed out"),
        ("browser_click", "sel=b", False, "clicked"),
    ]
    text = _facts(log, said="Opening the form.")
    assert text is not None
    assert "check_operation_status" not in text
    assert text.index("browser_open") < text.index("browser_click")
    assert "Opening the form." in text


def test_nothing_to_say():
    assert _facts([]) is None


def test_running_call_comes_last():
    text = _facts([("browser_open", "u", False, "ok")], pending={"x": ("browser_type", "text=hi")})
    assert text.index("browser_open") < text.index("browser_type")


def test_said_line_drops_questions_and_requests():
    text = _facts(
        [("browser_open", "u", False, "ok")],
        said="I found the form. Please log in to continue. Which option do you want?",
    )
    assert "I found the form." in text
    assert "Please" not in text and "Which option" not in text


def test_asks_user_filter():
    from app.plugins.voice_api import _ASKS_USER

    for bad in ("Please log in to the site.", "Введите код из письма.", "Вам нужно подтвердить вход.",
                "I opened the form. Now enter the code.", "Какой вариант выбрать?", "Can you check it?"):
        assert _ASKS_USER.search(bad), bad
    for good in ("I'm filling in the booking form now.", "Открыла страницу бронирования и заполняю форму.",
                 "Нашла три письма от Анны, сверяю даты.", "Still searching the web for the prices.",
                 "Подтверждение от сайта уже получено, оформляю заказ."):
        assert not _ASKS_USER.search(good), good

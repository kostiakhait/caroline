"""The final-text gate (app/own_identity_gate.py) and the ladder's draft rule. Run: python -m pytest test_own_identity_gate.py -q"""

from app import claude_ladder as cl
from app import own_identity_gate as g

BODY = "Hello Anita,\n\nThank you for your reply. I will come to your office at 1 PM today.\n\nBest regards,\nKonstantin Khait"
SHOWN_TEXT = (
    "Письмо пойдёт под вашим именем.\n\n**Кому:** AFrohlich@x.org\n\n> Hello Anita,\n>\n"
    "> Thank you for your reply. I will come to your office at 1 PM today.\n>\n> Best regards,\n> Konstantin Khait\n\nОтправить?"
)


def entry(role, text, ms, kind="shown"):
    return {"role": role, "text": text, "ms": ms, "kind": kind}


def test_a_quoted_draft_matches_the_bare_body():
    assert g.shown_entry([entry("assistant", SHOWN_TEXT, 10)], BODY) is not None


def test_a_changed_word_is_not_the_shown_text():
    assert g.shown_entry([entry("assistant", SHOWN_TEXT, 10)], BODY.replace("1 PM", "2 PM")) is None


def test_withheld_and_progress_notes_are_not_shown():
    for kind in ("withheld", "progress_note"):
        assert g.shown_entry([entry("assistant", SHOWN_TEXT, 10, kind)], BODY) is None


def test_the_incident_a_yes_before_the_draft_does_not_count():
    log = [entry("user", "ДА блядь, что ты за дура", 5), entry("assistant", SHOWN_TEXT, 10)]
    shown = g.shown_entry(log, BODY)
    answers = g.messages_after(log, shown["ms"])
    assert answers == []
    assert "has not answered" in g.missing_reason(BODY, shown, answers, False)


def test_never_shown_is_blocked_and_shown_plus_yes_goes():
    assert "not been shown" in g.missing_reason(BODY, None, [], False)
    log = [entry("assistant", SHOWN_TEXT, 10), entry("user", "да", 20)]
    shown = g.shown_entry(log, BODY)
    assert g.missing_reason(BODY, shown, g.messages_after(log, shown["ms"]), True) is None
    assert "not a yes" in g.missing_reason(BODY, shown, ["убери абзац"], False)


def test_html_body_and_body_keys():
    assert g.body_of({"to": "a", "html": "<p>Hi</p>"}) == "<p>Hi</p>"
    assert g.normalize("<p>Hi  <b>there</b></p>") == "hi there"
    assert g.body_of({"to": "a"}) == ""


def test_a_draft_waiting_for_a_yes_is_not_judged_by_the_ladder():
    turn = cl.LadderTurn(user_text="send it", rung=1)
    assert cl.awaits_user("Показываю текст.\n\n> Hello Anita\n\nОтправить в таком виде?")
    assert not cl.needs_verdict(cl.LadderTurn(user_text="x", rung=0), "Показываю.\n\n> Hello\n\nОтправить в таком виде?", False)
    assert not cl.awaits_user("Что вы имеете в виду?")          # a plain question is still judged
    assert not cl.awaits_user("> quoted\n\nГотово.")

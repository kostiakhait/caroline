"""The final-text gate (app/own_identity_gate.py) and the ladder's draft rule. Run: python -m pytest test_own_identity_gate.py -q"""

from app import action_ledger
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


def _may_send(log, sent_ms=0):
    shown, answers = g.approval_window(log, BODY, sent_ms)
    return g.missing_reason(BODY, shown, answers, "да" in [a.lower() for a in answers]) is None


def test_a_repeat_of_the_same_draft_keeps_the_yes_given_to_it():
    # 2026-11-27: shown, "Да", the same draft shown again -- and the gate blocked the send twice.
    log = [entry("assistant", SHOWN_TEXT, 10), entry("user", "Да", 20), entry("assistant", SHOWN_TEXT, 30)]
    assert g.approval_window(log, BODY)[1] == ["Да"]
    assert _may_send(log)
    log += [entry("user", "Да", 40), entry("assistant", SHOWN_TEXT, 50)]
    assert _may_send(log)


def test_a_yes_to_another_draft_in_between_does_not_count():
    other = "Вот другое письмо:\n\n> Hello Boris, see you on Friday.\n\nОтправить?"
    log = [entry("assistant", SHOWN_TEXT, 10), entry("assistant", other, 20), entry("user", "Да", 30),
           entry("assistant", SHOWN_TEXT, 40)]
    assert g.approval_window(log, BODY)[1] == []


def test_a_plain_note_between_the_showings_changes_nothing():
    log = [entry("assistant", SHOWN_TEXT, 10), entry("user", "Да", 20), entry("assistant", "Готовлю отправку.", 25),
           entry("assistant", SHOWN_TEXT, 30)]
    assert _may_send(log)


def test_one_yes_is_one_send():
    log = [entry("assistant", SHOWN_TEXT, 10), entry("user", "Да", 20), entry("assistant", SHOWN_TEXT, 40)]
    assert g.approval_window(log, BODY, sent_ms=30)[1] == []
    assert not _may_send(log, sent_ms=30)


def test_a_showing_older_than_a_day_does_not_count():
    day = g.SAME_TEXT_WINDOW_MS
    log = [entry("assistant", SHOWN_TEXT, 10), entry("user", "Да", 20), entry("assistant", SHOWN_TEXT, day + 100)]
    assert g.approval_window(log, BODY)[1] == []


def test_the_ledger_tells_when_the_text_last_went_out(tmp_path):
    assert action_ledger.last_sent_ms(str(tmp_path), BODY, g.normalize) == 0
    action_ledger.append(str(tmp_path), "1", "email_send", {"to": "a@x.org", "subject": "s", "text": BODY})
    assert action_ledger.last_sent_ms(str(tmp_path), BODY, g.normalize) > 0
    assert action_ledger.last_sent_ms(str(tmp_path), "Something else", g.normalize) == 0


def test_a_draft_followed_by_a_request_for_the_word_waits_for_the_owner():
    assert cl.awaits_user("Текст:\n\n> Hello Anita\n\nОтправляю после вашего «да».")
    assert cl.awaits_user("> Hello\n\nОтправить? Жду ответа.")
    assert not cl.awaits_user("> Hello\n\nПисьмо отправлено.")


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


def test_quoted_draft_is_split_out_so_it_is_never_translated():
    text = "Вот письмо:\n\n> Hi Krunal,\n> Thanks for reaching out.\n\nОтправить?"
    runs = g.split_quoted(text)
    assert [q for q, _ in runs] == [False, True, False]
    assert runs[1][1] == "> Hi Krunal,\n> Thanks for reaching out.\n\n"[:len(runs[1][1])] or "Hi Krunal" in runs[1][1]
    assert "".join(c for _, c in runs) == text
    # shown verbatim, the English body matches what email_send carries
    log = [entry("assistant", text, 10)]
    assert g.shown_entry(log, "Hi Krunal,\nThanks for reaching out.") is not None

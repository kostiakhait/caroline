"""email_get_thread's pure part (app/email_thread.py): the thread of 2026-11-27 (tab 1, "Re: Декабрь") as it stood."""
from __future__ import annotations

from app import email_thread as et

ME = "kostia.khait@gmail.com"
OKSANA = "Oksana Khait <khait.oksan@gmail.com>"


def letter(uid, folder, sender, to, date, subject, html=None, text=None):
    return {"uid": str(uid), "folder": folder, "from": sender, "to": to, "date": date, "subject": subject, "html": html, "text": text}


QUOTE = ('<div dir="ltr" class="gmail_attr">On Fri, Nov 27, 2026 at 3:17 PM &lt;x&gt; wrote:<br></div>'
         '<blockquote class="gmail_quote">Оксана, здравствуйте.<br>1. старое</blockquote>')

THREAD = [
    letter(99876, "INBOX", OKSANA, ME, "Sun, 22 Nov 2026 10:00:00 -0500", "Re: Декабрь", text="В любой день 16:30."),
    letter(100223, "INBOX", OKSANA, ME, "Fri, 27 Nov 2026 15:17:00 -0500", "Re: Декабрь",
           html="<div><p>Праздник перенесли на воскресенье.</p></div>" + QUOTE),
    letter(5348, "[Gmail]/Sent Mail", f"Kostia Khait <{ME}>", OKSANA, "Fri, 27 Nov 2026 15:10:42 -0500", "Re: Декабрь",
           text="Оксана, здравствуйте.\n1. Уточните день"),
    letter(100230, "INBOX", OKSANA, ME, "Fri, 27 Nov 2026 15:58:00 -0500", "Re: Декабрь",
           html="<p>Отправляю контакты Warrior Tech.</p>" + QUOTE),
    letter(5349, "[Gmail]/Sent Mail", f"Kostia Khait <{ME}>", OKSANA, "Fri, 27 Nov 2026 16:02:11 -0500", "Re: Декабрь",
           html='<div dir="auto">Я могу передать Мишель тёте Лене.</div>' + QUOTE),
]
LEDGER = [{"ms": 1795810244000, "when": "2026-11-27T15:10:44-05:00", "tool": "email_send_reply",
           "to": "khait.oksan@gmail.com", "subject": "Re: Декабрь", "text": "Оксана, здравствуйте. 1. Уточните день"}]


def test_subject_key_ignores_reply_prefixes():
    assert et.subject_key("Re: RE[2]: Ответ: Декабрь ") == "декабрь"
    assert et.subject_key("Fwd: Декабрь") == et.subject_key("Декабрь")


def test_a_letter_shows_only_its_own_words_not_the_quoted_history():
    own = et.own_text(THREAD[1])
    assert "перенесли на воскресенье" in own
    assert "старое" not in own and "wrote" not in own
    assert et.own_text(THREAD[4]) == "Я могу передать Мишель тёте Лене."


def test_plain_text_quotes_are_cut_too():
    msg = letter(1, "INBOX", OKSANA, ME, "", "Re: X", text="Да, хорошо.\n\nOn Fri, Nov 27, 2026 at 3:10 PM <a@b.c> wrote:\n> старое\n> ещё")
    assert et.own_text(msg) == "Да, хорошо."


def test_the_other_side_is_named_as_the_other_side():
    assert "other side" in et.author_of(THREAD[1], ME, LEDGER)


def test_a_letter_caroline_sent_is_marked_as_hers():
    assert "YOU, Caroline" in et.author_of(THREAD[2], ME, LEDGER)


def test_the_owners_own_letter_is_marked_as_his_and_not_a_mystery():
    """2026-11-27 16:02: the owner's answer, written on his phone, was taken for a letter to investigate."""
    note = et.author_of(THREAD[4], ME, LEDGER)
    assert "WITHOUT you" in note and "not something to investigate" in note


def test_the_thread_is_kept_by_subject_and_the_other_side():
    counterparts = {"khait.oksan@gmail.com"}
    key = et.subject_key("Re: Декабрь")
    assert all(et.same_thread(m, key, counterparts) for m in THREAD)
    assert not et.same_thread(letter(7, "INBOX", "Leah <leah@v.org>", ME, "", "Re: Декабрь"), key, counterparts)
    assert not et.same_thread(letter(8, "INBOX", OKSANA, ME, "", "Ninja"), key, counterparts)


def test_the_rendered_thread_is_oldest_first_and_ends_with_the_latest_letter():
    ordered = sorted(THREAD, key=et.message_ms)
    assert [m["uid"] for m in ordered] == ["99876", "5348", "100223", "100230", "5349"]
    text, rest = et.render(ordered, ME, LEDGER, 0, 100_000)
    assert rest == -1
    assert text.index("Letter 1 of 5") < text.index("Letter 4 of 5") < text.index("Letter 5 of 5")
    assert text.rstrip().endswith("Я могу передать Мишель тёте Лене.")


def test_a_long_thread_comes_in_pages_and_the_page_says_where_to_go_on():
    ordered = sorted(THREAD, key=et.message_ms)
    first, rest = et.render(ordered, ME, LEDGER, 0, 600)
    assert 0 < rest < len(ordered)
    second, rest2 = et.render(ordered, ME, LEDGER, rest, 100_000)
    assert rest2 == -1 and f"Letter {rest + 1} of 5" in second


def test_an_html_letter_is_given_to_the_reminder_extractor_with_its_words_and_date():
    seen = et.letter_for_reminders(THREAD[1])
    assert "перенесли на воскресенье" in seen and "27 Nov 2026" in seen and "старое" not in seen


def test_the_extractor_reads_a_thread_newest_first_and_a_new_letter_changes_its_start():
    ordered = sorted(THREAD, key=et.message_ms)
    now, before = et.thread_for_reminders(ordered), et.thread_for_reminders(ordered[:-1])
    assert now.index("тёте Лене") < now.index("Warrior Tech") < now.index("перенесли на воскресенье")
    assert now[:200] != before[:200]
    short = et.thread_for_reminders(ordered, budget_chars=200)
    assert "тёте Лене" in short and "Warrior Tech" not in short

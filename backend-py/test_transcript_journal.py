"""The append-only transcript journal and the reconcile that brings a window back its lost entries (2026-10-30).
Run: python -m pytest test_transcript_journal.py -q"""

from app import transcript_journal as tj

DAY = 24 * 3600 * 1000


def e(role, text, ts):
    return {"role": role, "text": text, "ts": ts, "attachments": []}


def test_append_is_idempotent_and_never_rewrites(tmp_path):
    ws = str(tmp_path)
    assert tj.append_entries(ws, "1", [e("user", "привет", 1000), e("assistant", "здравствуйте", 2000)]) == 2
    assert tj.append_entries(ws, "1", [e("user", "привет", 1000)]) == 0
    assert [x["text"] for x in tj.read_entries(ws, "1")] == ["привет", "здравствуйте"]


def test_unusable_entries_are_ignored(tmp_path):
    ws = str(tmp_path)
    assert tj.append_entries(ws, "1", [{"role": "system", "text": "x"}, e("user", "  ", 1), "junk", e("user", "ok", 5)]) == 1


def test_a_line_cut_by_a_crash_is_skipped_and_the_next_append_survives(tmp_path):
    ws = str(tmp_path)
    tj.append_entries(ws, "1", [e("user", "первое", 1000)])
    with tj.journal_path(ws, "1").open("a", encoding="utf-8") as f:
        f.write('{"role": "assistant", "text": "обор')  # power cut mid-line, no newline
    tj._keys.clear()  # a new process
    assert tj.append_entries(ws, "1", [e("user", "второе", 3000)]) == 1
    assert [x["text"] for x in tj.read_entries(ws, "1")] == ["первое", "второе"]


def test_journal_survives_a_new_process(tmp_path):
    ws = str(tmp_path)
    tj.append_entries(ws, "1", [e("user", "a", 1000)])
    tj._keys.clear()
    assert tj.append_entries(ws, "1", [e("user", "a", 1000)]) == 0


def test_missing_returns_what_the_window_lost_in_time_order():
    journal = [e("user", "вчера", DAY), e("assistant", "ответ вчера", DAY + 10), e("user", "сегодня", 2 * DAY), e("assistant", "ответ сегодня", 2 * DAY + 10)]
    window = [e("user", "вчера", DAY + 3000), e("assistant", "ответ вчера", DAY + 3010)]
    assert [m["text"] for m in tj.missing_for_client(journal, window)] == ["сегодня", "ответ сегодня"]


def test_an_entry_the_window_has_with_a_slightly_different_ts_is_not_missing():
    assert tj.missing_for_client([e("user", "да", 10_000)], [e("user", "да", 15_000)]) == []


def test_same_text_far_apart_is_a_different_entry():
    journal = [e("user", "да", DAY), e("user", "да", 2 * DAY)]
    window = [e("user", "да", DAY)]
    assert [m["ts"] for m in tj.missing_for_client(journal, window)] == [2 * DAY]


def test_entries_older_than_the_windows_oldest_are_not_brought_back():
    journal = [e("user", "очень давно", 100), e("user", "в окне", 5 * DAY)]
    window = [e("user", "в окне", 5 * DAY)]
    assert tj.missing_for_client(journal, window) == []


def test_text_whitespace_differences_do_not_matter():
    assert tj.missing_for_client([e("assistant", "a  b\nc", 1000)], [e("assistant", "a b c", 1000)]) == []


def test_session_file_noise_is_not_journaled_and_the_time_stamp_is_dropped(tmp_path):
    ws = str(tmp_path)
    noise = ["<command-name>/model</command-name>", "<local-command-stdout>Set model</local-command-stdout>",
             "[Request interrupted by user]", "You've hit your session limit · resets 3:10am", "[[NO_UPDATE]]"]
    assert tj.append_entries(ws, "1", [e("user", n, 1000 + i) for i, n in enumerate(noise)]) == 0
    assert tj.append_entries(ws, "1", [e("user", "[Sent: Fri, Oct 30, 2026, 12:43 PM EDT] ⁣привет", 9)]) == 1
    assert tj.read_entries(ws, "1")[0]["text"] == "привет"
    assert tj.missing_for_client(tj.read_entries(ws, "1"), [e("user", "привет", 9)]) == []

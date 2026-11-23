"""What local edge-tts is given to say (app/local_edge_tts.py) and the Sec-MS-GEC token (app/edge_tts_token_patch.py);
neither had a test (2026-11-22). Run: python -m pytest test_local_edge_tts_text.py -q"""

import re
from datetime import datetime, timezone

import pytest

from app import edge_tts_token_patch as tok
from app import local_edge_tts as tts

LONG = "Длинный список без точек " * 8 + "конец."


def test_a_long_sentence_does_not_jump_ahead_of_the_one_before_it():
    """2026-11-22: "Первое." was held back and spoken after the long sentence that followed it."""
    chunks = tts._chunk_by_sentences(f"Первое. {LONG} Последнее.", 60)
    assert chunks[0] == "Первое." and chunks[-1] == "Последнее."
    assert " ".join(chunks) == re.sub(r"\s+", " ", f"Первое. {LONG} Последнее.").strip()


def test_a_long_sentence_is_cut_between_words():
    chunks = tts._chunk_by_sentences(LONG, 60)
    words = set(LONG.split())
    assert all(len(c) <= 60 for c in chunks)
    assert all(w in words for c in chunks for w in c.split())          # no word cut in two
    assert tts._split_between_words("a" * 25 + " b", 10) == ["a" * 10, "a" * 10, "aaaaa b"]   # only an overlong word is


def test_sentences_are_packed_up_to_the_limit_in_order():
    text = "Один. Два. Три. Четыре."
    assert tts._chunk_by_sentences(text, 1400) == [text]
    assert tts._chunk_by_sentences(text, 10) == ["Один. Два.", "Три.", "Четыре."]
    assert tts._chunk_by_sentences("   ", 10) == []


def test_parallel_parts_keep_short_sentences_with_a_neighbour():
    text = "Да. Нашла три магазина с нужной моделью телефона. Во втором дешевле всего на тысячу гривен."
    parts = tts._parallel_parts(text)
    assert parts == ["Да. Нашла три магазина с нужной моделью телефона.", "Во втором дешевле всего на тысячу гривен."]


def test_sanitize_removes_what_makes_the_service_return_no_audio():
    raw = "«Цитата» — тире и​невидимое­, “quotes” − 1\x07"
    assert tts._sanitize_text(raw) == '"Цитата" - тире иневидимое, "quotes" - 1'


def test_signed_prosody_values():
    assert tts._fmt_signed(0, "%") == "+0%" and tts._fmt_signed(-5, "Hz") == "-5Hz"


def test_the_token_has_the_v2_shape_and_changes_only_every_five_minutes(monkeypatch):
    class Clock:
        now_value = datetime(2026, 11, 22, 12, 0, 1, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.now_value

    real_datetime = tok.datetime

    class FakeDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return Clock.now_value

    monkeypatch.setattr(tok, "datetime", FakeDatetime)
    first = tok.generate_sec_ms_gec_v2("6A5AA1D4EAFF4E9FB37E23D68491D6F4")
    assert re.fullmatch(r"[0-9A-F]{16}-[0-9A-F]{64}", first)
    Clock.now_value = datetime(2026, 11, 22, 12, 4, 59, tzinfo=timezone.utc)
    assert tok.generate_sec_ms_gec_v2("6A5AA1D4EAFF4E9FB37E23D68491D6F4") == first          # same 5-minute window
    Clock.now_value = datetime(2026, 11, 22, 12, 5, 0, tzinfo=timezone.utc)
    assert tok.generate_sec_ms_gec_v2("6A5AA1D4EAFF4E9FB37E23D68491D6F4") != first          # the next one
    assert tok.generate_sec_ms_gec_v2("OTHER") != tok.generate_sec_ms_gec_v2("6A5AA1D4EAFF4E9FB37E23D68491D6F4")


def test_filetime_buckets():
    unix_us = 1_763_812_801_000_000            # 2025-11-22 12:00:01 UTC
    ticks = tok._filetime_ticks_from_unix_us(unix_us)
    assert ticks % (tok._WINDOW_US * 10) == 0 and ticks <= (unix_us + tok._UNIX_TO_FILETIME_US) * 10


def test_apply_patches_edge_tts_once():
    edge_tts = pytest.importorskip("edge_tts")
    import edge_tts.communicate as communicate
    from edge_tts.drm import DRM

    tok.apply()
    tok.apply()
    assert communicate.SEC_MS_GEC_VERSION == f"2-{tok.CURRENT_EDGE_VERSION}"
    assert re.fullmatch(r"[0-9A-F]{16}-[0-9A-F]{64}", DRM.generate_sec_ms_gec())
    assert edge_tts is not None

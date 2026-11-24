"""Speech pieces (2026-11-24): a short reply of more than one sentence is read in pieces too, so its first sentence is
heard without waiting for the rest; speech_ahead.pieces_for and chat.js's speechPieces give the same pieces.
Run: python -m pytest test_speech_pieces.py -q"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import speech_ahead as sa

CHAT_JS = Path(__file__).resolve().parent.parent / "Windows" / "Caroline" / "wwwroot" / "chat.js"
FIRST = "Завтра в Киеве будет плюс пять градусов, без осадков, и ветер будет совсем слабый."
SHORT_TWO = FIRST + " Вечером похолодает до нуля, возьмите шарф."
SHORT_ONE = "Завтра в Киеве будет плюс пять градусов, без осадков и с очень слабым ветром весь день, до самого вечера."
TINY = "Готово. Отправила письмо Анне."
LONG = " ".join(f"Предложение номер {i}, в нём есть немного слов для длины." for i in range(8))


def test_a_short_reply_of_two_sentences_is_read_in_two_pieces():
    assert 120 <= len(SHORT_TWO) <= sa.PIECE_MAX_CHARS
    assert len(FIRST) >= sa.FIRST_PIECE_MIN_CHARS
    assert sa.pieces_for(SHORT_TWO) == [FIRST, "Вечером похолодает до нуля, возьмите шарф."]


def test_one_sentence_or_a_tiny_reply_stays_whole():
    assert sa.pieces_for(SHORT_ONE) == [SHORT_ONE]
    assert sa.pieces_for(TINY) == [TINY]                                   # below SPLIT_SHORT_MIN_CHARS


def test_a_long_reply_is_split_as_before():
    pieces = sa.pieces_for(LONG)
    assert len(pieces) >= 2 and pieces[0].startswith("Предложение номер 0") and len(pieces[0]) >= sa.FIRST_PIECE_MIN_CHARS


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_chat_js_reads_the_same_pieces():
    src = CHAT_JS.read_text(encoding="utf-8")
    start = src.index("  const SPEECH_PIECE_MAX_CHARS")
    end = src.index("  function speak(")
    block = src[start:end]
    texts = [SHORT_TWO, SHORT_ONE, TINY, LONG]
    script = block + "\nconst texts = " + json.dumps(texts, ensure_ascii=False) + ";\n" \
        "console.log(JSON.stringify(texts.map(t => speechPieces(t, SPEECH_PIECE_MAX_CHARS) || [t])));\n"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    js = json.loads(out.stdout)
    for text, pieces in zip(texts, js):
        expected = sa.split_for_speech(text) if len(sa.pieces_for(text)) > 1 or len(text) > sa.PIECE_MAX_CHARS else [text]
        assert pieces == expected, text[:40]
        assert pieces[: sa.PIECES_AHEAD] == [p for p in sa.pieces_for(text) if p in pieces][: sa.PIECES_AHEAD] or pieces == [text]
    assert re.search(r"SPEECH_SPLIT_SHORT_MIN_CHARS = %d;" % sa.SPLIT_SHORT_MIN_CHARS, src)

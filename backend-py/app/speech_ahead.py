"""Speech for a reply, made before the reply is final.

A voice reply is read out in pieces: chat.js splits it (splitForSpeech) and asks the
"tts" control op for each piece in turn. Measured live 2026-10-14, a weather answer:
ready at 19:05:55.9, its first piece audible at 19:06:02.6 -- 6.7s, most of it the
model call that spells "13°C" out in words (clean_text_for_speech) before the piece is
synthesized. During those seconds there is nothing to hear, right after the last
narration remark ended: an 11s silence just before the answer.

The text that goes out is most often already known seconds earlier: the fast lane's
answer inside the merge window, a tier's answer while the verifier checks it (it says
"OK" to most). So the first pieces of that text are split exactly as chat.js will split
them and synthesized right away (prepare). When chat.js then asks for a piece with
exactly that text, the "tts" op takes the result instead of starting over (take).
A piece of a text that did not go out is simply never asked for and expires.
"""

from __future__ import annotations

import asyncio
import re
import time

from app.logging_setup import log_event

# chat.js: SPEECH_PIECE_MAX_CHARS, VISUAL_SPEECH_PIECE_MAX_CHARS, SPEECH_FIRST_PIECE_MIN_CHARS.
PIECE_MAX_CHARS = 240
VISUAL_PIECE_MAX_CHARS = 300
FIRST_PIECE_MIN_CHARS = 60
# The first two pieces: the second is what chat.js prefetches while the first plays.
PIECES_AHEAD = 2
AHEAD_TTL_S = 120.0
AHEAD_MAX = 12

_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

_ahead: dict[str, tuple[float, asyncio.Task[str]]] = {}


def split_for_speech(text: str, max_chars: int = PIECE_MAX_CHARS) -> list[str]:
    """chat.js's splitForSpeech, line for line. Any difference only costs a miss."""
    sentences = [
        s.strip()
        for line in re.split(r"\n+", text)
        for s in _SENTENCE_END.split(_LIST_MARKER.sub("", line, count=1))
        if s.strip()
    ]
    pieces: list[str] = []
    for sentence in sentences:
        last = pieces[-1] if pieces else ""
        first_too_short = len(pieces) == 1 and len(last) < FIRST_PIECE_MIN_CHARS
        if (len(pieces) > 1 or first_too_short) and len(last) + 1 + len(sentence) <= max_chars:
            pieces[-1] = f"{last} {sentence}"
        else:
            pieces.append(sentence)
    return pieces or [text]


def pieces_for(text: str) -> list[str]:
    """The pieces chat.js may ask for, for a reply spoken on its own: the reply whole
    when it is short, else its first pieces as split for plain audio and for Visual
    Mode (which one applies is chat.js's to know; asking for both costs a call at most)."""
    if len(text) <= PIECE_MAX_CHARS:
        return [text]
    seen: list[str] = []
    for max_chars in (VISUAL_PIECE_MAX_CHARS, PIECE_MAX_CHARS):
        for piece in split_for_speech(text, max_chars)[:PIECES_AHEAD]:
            if piece not in seen:
                seen.append(piece)
    return seen


async def synthesize_piece(text: str, gender: str | None, language_iso: str | None) -> str:
    """What the "tts" control op does for one piece (main.py), base64 audio."""
    from app.login_api import get_cached_v2_session
    from app.plugins.voice_api import clean_text_for_speech, speech_needs_cleanup, synthesize_speech, voice_for_gender

    session = await get_cached_v2_session() if speech_needs_cleanup(text) else None
    cleaned = await clean_text_for_speech(text, session)
    return await synthesize_speech(cleaned, voice_for_gender(gender), session, language=language_iso,
                                   session_provider=get_cached_v2_session)


def prepare(text: str, gender: str | None, language_iso: str | None) -> None:
    """Starts synthesis of the first pieces of a reply that will most likely be read out."""
    now = time.monotonic()
    for key in [k for k, (at, task) in _ahead.items() if now - at > AHEAD_TTL_S]:
        _ahead.pop(key)[1].cancel()
    for piece in pieces_for(text.strip()):
        if piece in _ahead or len(_ahead) >= AHEAD_MAX:
            continue
        task = asyncio.create_task(synthesize_piece(piece, gender, language_iso))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)  # never "never retrieved"
        _ahead[piece] = (now, task)
    log_event("engine", "speech_ahead_started", pieces=len(pieces_for(text.strip())), text_len=len(text))


def take(text: str) -> asyncio.Task[str] | None:
    """The synthesis already under way for exactly this piece, if there is one."""
    entry = _ahead.pop(text, None)
    if entry is None or time.monotonic() - entry[0] > AHEAD_TTL_S:
        return None
    return entry[1]


def discard_all() -> None:
    for _at, task in _ahead.values():
        task.cancel()
    _ahead.clear()

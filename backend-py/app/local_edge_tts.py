"""In-process Microsoft Edge TTS (via the edge-tts package) -- ported from reforce's own
EdgeTTS.py (d:/REPO/reforce/EdgeTTS.py), Camerlengo's own hardened wrapper around the same
edge_tts library, rather than reinventing a thinner one here.

Replaces local_tts_server.py/local_tts_launcher.py's separate HTTP-server-in-a-subprocess
(2026-09-27, per explicit instruction, after a real incident): that whole subprocess+HTTP
indirection was ported as-is from the OLD Node.js backend (backend/src/localTtsServer.ts),
where it existed for a real reason -- Node.js has no way to call a Python library directly,
so a separate Python process bridged over HTTP was the only option. backend-py IS already
Python, so that reason no longer applies: this module is called directly, in-process, with
a normal `await`, no subprocess to launch/supervise, no HTTP round trip, and no second
asyncio event loop being created per call (the old server's do_POST did a fresh
`asyncio.run()` on its OWN worker thread for every single request).

That old subprocess's design also had no timeout at all on the underlying edge_tts call --
confirmed live, 2026-09-27: a stuck request just left a worker thread hanging forever
(ThreadingHTTPServer spawns one unbounded thread per connection), and after ~24 minutes of
real use in one session, every subsequent call started failing "Server disconnected without
sending a response" -- 0 of 14 real requests succeeded locally that session, every one
paying Camerlengo's own slow ai:tts fallback (20-97s each, even for a few words). This
module's real connect_timeout/receive_timeout (below) is exactly reforce's own fix for
that same failure mode, ported over rather than re-derived.

Bug fix (no exponential backoff): reforce's own EdgeTTS.py backs off exponentially between
retries (baseBackoffSec * 2**attempt) -- per this codebase's own standing rule ("every retry
mechanism in Caroline's backend is flat, never exponential, even when the original code
being ported used exponential backoff"), flattened to a fixed delay here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("local_edge_tts")


class _NoAudioError(RuntimeError):
    """edge-tts returned an empty audio stream -- Microsoft's own soft rate-limit, not a
    real failure of the request itself; worth a short, separate backoff before retrying."""


@dataclass
class EdgeTtsOptions:
    voice: str = "ru-RU-SvetlanaNeural"
    rate_percent: int = 0
    pitch_hz: int = 0
    volume_percent: int = 0
    max_chunk_chars: int = 1400
    max_retries: int = 3
    retry_delay_s: float = 1.0
    no_audio_retry_delay_s: float = 2.0
    connect_timeout_s: int = 10
    # Default in edge-tts itself is 60s; reforce raised this for long chunks -- carried
    # over unchanged, this is exactly the bound that was missing entirely before.
    receive_timeout_s: int = 180


async def synthesize_to_bytes(text: str, options: EdgeTtsOptions | None = None) -> bytes:
    """The one entry point this module needs -- sanitizes, chunks by sentence, synthesizes
    each chunk (with retries), and concatenates the resulting MP3 bytes."""
    opts = options or EdgeTtsOptions()
    text = _sanitize_text(text)
    chunks = _chunk_by_sentences(text, opts.max_chunk_chars)
    if not chunks:
        return b""
    parts = [await _synth_chunk(chunk, opts) for chunk in chunks]
    return b"".join(parts)


# One request takes about as long as its text is: measured 2026-10-11 on text the service
# had not seen before (a repeated text comes back from its cache in 0.5s and says
# nothing), 89 chars took 1.2s, 162 chars 2.2s, 285 chars 4.3s -- and live, a 218-char
# reply waited 7.4s for its audio. The four sentences of a 285-char piece sent at once
# were all back in 1.3-1.7s, the joined audio within 2% of the whole text's length, so
# no pauses are added between them.
PARALLEL_PART_MIN_CHARS = 40
# Across every text being synthesized at the moment: empty audio is Microsoft's soft
# rate limit (see _NoAudioError), and each hit costs a retry delay.
_PARALLEL_REQUESTS = asyncio.Semaphore(6)


def _parallel_parts(text: str) -> list[str]:
    """Whole sentences; a short one stays with its neighbour -- a request of its own
    for "Да." buys nothing."""
    parts: list[str] = []
    for sentence in (p.strip() for p in _SENT_SPLIT_RE.split(text)):
        if not sentence:
            continue
        if parts and (len(parts[-1]) < PARALLEL_PART_MIN_CHARS or len(sentence) < PARALLEL_PART_MIN_CHARS):
            parts[-1] = f"{parts[-1]} {sentence}"
        else:
            parts.append(sentence)
    return parts


async def synthesize_sentences_in_parallel(text: str, options: EdgeTtsOptions | None = None) -> bytes:
    """synthesize_to_bytes for speech someone is waiting to hear: the text's sentences
    are synthesized side by side and joined in order. Any part failing fails the whole
    call, like synthesize_to_bytes itself."""
    opts = options or EdgeTtsOptions()
    parts = _parallel_parts(_sanitize_text(text))
    if len(parts) < 2:
        return await synthesize_to_bytes(text, opts)

    async def one(part: str) -> bytes:
        async with _PARALLEL_REQUESTS:
            return await synthesize_to_bytes(part, opts)

    return b"".join(await asyncio.gather(*(one(part) for part in parts)))


class EdgeTtsRefused(RuntimeError):
    """The service refused the connection itself (HTTP 403 on the handshake)."""


# Live, 2026-10-12: after an evening of 60 syntheses that all worked, every request from
# this machine was answered 403 -- the read-aloud endpoint is free and unofficial, and it
# limits by address. Each remark then spent ~5s on three retries that could not succeed
# before the server-side fallback was even tried: the first spoken word of a turn came at
# 18s instead of 6s. A refusal is not a glitch to retry through, and more requests are the
# one thing that does not help with a limiter. So: no retry on a 403, and for the next
# while every call fails at once, which sends the caller (voice_api.synthesize_speech)
# straight to its fallback. After that one request tries again.
REFUSED_COOLDOWN_S = 600.0
_refused_until = 0.0


def _refused_now() -> bool:
    import time

    return time.monotonic() < _refused_until


def _note_refused() -> None:
    import time

    global _refused_until
    _refused_until = time.monotonic() + REFUSED_COOLDOWN_S


async def _synth_chunk(text: str, opts: EdgeTtsOptions) -> bytes:
    if not text or len(text.strip()) < 2:
        return b""
    if _refused_now():
        raise EdgeTtsRefused("local edge-tts was refused by the service (403) a short while ago; not asking again yet")

    import edge_tts

    rate = _fmt_signed(opts.rate_percent, "%")
    pitch = _fmt_signed(opts.pitch_hz, "Hz")
    volume = _fmt_signed(opts.volume_percent, "%")

    attempt = 0
    last_exc: Exception | None = None
    while attempt <= opts.max_retries:
        try:
            communicate = edge_tts.Communicate(
                text, opts.voice, rate=rate, pitch=pitch, volume=volume,
                connect_timeout=opts.connect_timeout_s, receive_timeout=opts.receive_timeout_s,
            )
            audio = bytearray()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    audio.extend(chunk["data"])
            if not audio:
                raise _NoAudioError("edge-tts returned empty audio")
            return bytes(audio)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- classified into a retry/give-up decision below
            if getattr(exc, "status", None) == 403:
                _note_refused()
                raise EdgeTtsRefused(f"local edge-tts was refused by the service: {exc}") from exc
            last_exc = exc
            attempt += 1
            if attempt > opts.max_retries:
                break
            delay = opts.no_audio_retry_delay_s if isinstance(exc, _NoAudioError) else opts.retry_delay_s
            log.warning("local_edge_tts retry %d/%d in %.1fs: %s", attempt, opts.max_retries, delay, exc)
            await asyncio.sleep(delay)

    raise RuntimeError(f"local edge-tts synthesis failed after {opts.max_retries} retries: {last_exc}") from last_exc


def _fmt_signed(value: int, unit: str) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value}{unit}"


# Split after sentence-ending punctuation + whitespace + any uppercase letter (Unicode) --
# ported verbatim from reforce's EdgeTTS.py, including its own comment on why: \p{Lu} isn't
# available in stdlib re, so this spells out the uppercase ranges it actually needs (Latin,
# Latin Extended, Cyrillic, digits) instead.
_SENT_SPLIT_RE = re.compile(
    r"(?<=[.!?…])\s+(?=[A-ZА-ЯЁÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞ"
    r"ĀĂĄĆĈĊČĎĐĒĔĖĘĚĜĞĠĢĤĦĨĪĬĮIĲĴĶĹĻĽĿŁŃŅŇŊŌŎŐŒŔŖŘŚŜŞŠŢŤŦŨŪŬŮŰŲŴŶŽǄǇǊ"
    r"0-9])",
    re.UNICODE,
)


def _sanitize_text(text: str) -> str:
    """Removes/replaces characters that make edge-tts silently return NO audio at all
    ("No audio was received") -- ported verbatim from reforce's EdgeTTS.py: smart quotes,
    em/en-dashes, soft hyphens, zero-width characters, non-breaking/special spaces, and
    control characters other than tab/LF/CR."""
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u00ab", '"').replace("\u00bb", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201a", ",").replace("\u201b", "'")
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2015", "-")
    text = text.replace("\u2212", "-")
    text = text.replace("\u00ad", "")
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"):
        text = text.replace(ch, "")
    for ch in ("\u00a0", "\u202f", "\u2007", "\u2008", "\u2009", "\u200a",
               "\u3000", "\u2002", "\u2003", "\u2004", "\u2005", "\u2006"):
        text = text.replace(ch, " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    return text


def _chunk_by_sentences(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    sentences = [p.strip() for p in _SENT_SPLIT_RE.split(text) if p.strip()]
    if not sentences:
        return [text[:max_chars]]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        sentence_len = len(sentence)
        if sentence_len > max_chars:
            for start in range(0, sentence_len, max_chars):
                piece = sentence[start:start + max_chars].strip()
                if piece:
                    chunks.append(piece)
            continue
        if current_len + (1 if current else 0) + sentence_len <= max_chars:
            current.append(sentence)
            current_len += (1 if current_len else 0) + sentence_len
        else:
            if current:
                chunks.append(" ".join(current))
            current = [sentence]
            current_len = sentence_len
    if current:
        chunks.append(" ".join(current))
    return chunks

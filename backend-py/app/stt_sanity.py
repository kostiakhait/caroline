"""A pass over what speech recognition returned, before it is shown or sent anywhere.

Per explicit instruction (2026-10-11): besides the recognition itself, a microagent
checks the result for meaning and coherence. Laid out the way the microagents paper
has it -- deterministic checks first, then the cheapest model, and what the model
returns is itself checked before it is trusted:

1. Code. What is known never to be speech: Whisper's own inventions on silence and
   noise ("Субтитры создавал DimaTorzok", "Продолжение следует...", "Thanks for
   watching"), a word or phrase looping, nothing but punctuation, a script that is
   not the conversation's. These cost no tokens and no time.
2. The cheapest model, for what code cannot see: is this a meaningful, connected
   utterance in this conversation, and if one word is plainly a mishearing ("по лохе"
   for "по логике"), which word was meant. It answers OK, FIX with the corrected
   text, or GARBAGE.
3. Code again. A FIX is taken only if it stays close to what was heard: the model is
   there to repair a word, not to rewrite what the user said.

Any failure of the model step leaves the transcript exactly as recognition gave it:
this pass may remove noise and repair a word, it must never lose a real utterance.
"""

from __future__ import annotations

import asyncio
import difflib
import re
import time
from dataclasses import dataclass

from app.logging_setup import log_event

# Same cheap first-step model as the fast lane's gate and the narrator's first drafter
# (small_model_engine.py). Picked there by benchmark; here see evals/stt_sanity_eval.py.
STT_SANITY_MODEL = "google/gemini-2.5-flash-lite"
STT_SANITY_TIMEOUT_S = 3.0
# Short commands and answers ("да", "стоп", "спасибо") are left to step 1: there is
# nothing in them for a model to judge, and they are the ones that must not wait.
STT_SANITY_MODEL_MIN_WORDS = 3
# A FIX must stay this close to what was heard (difflib ratio over lowercased text).
STT_SANITY_FIX_MIN_SIMILARITY = 0.6
# A word or short phrase said this many times in a row is the recognizer looping.
_LOOP_MIN_REPEATS = 4

# Whisper's well-known output for silence and background noise -- credits and sign-offs
# of the videos it was trained on. Each is matched against the WHOLE transcript: a real
# sentence that merely contains one of these words is left alone.
_HALLUCINATIONS = re.compile(
    r"^\W*(?:"
    r"субтитры\s+(?:сделал|создавал|создал|делал|подготовил)\w*\s+\S+(?:\s+\S+)?"
    r"|редактор\s+субтитров\b.*"
    r"|корректор\s+\S+(?:\s+\S+)?"
    r"|продолжение\s+следует"
    r"|спасибо\s+за\s+(?:просмотр|внимание)"
    r"|подписывайтесь\s+на\s+(?:наш\s+)?канал.*"
    r"|ставьте\s+лайки?.*"
    r"|дякую\s+за\s+(?:перегляд|увагу)"
    r"|thanks?\s+(?:you\s+)?for\s+watching.*"
    r"|subtitles\s+by\b.*"
    r"|(?:\[|\()\s*(?:музыка|music|аплодисменты|applause|смех|laughter|тишина|silence)\s*(?:\]|\))"
    r")\W*$",
    re.IGNORECASE | re.DOTALL,
)
_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)
# What the recognizer returns for plain silence (2026-10-11, the app's own cloud
# recognizer on 4s of digital silence: "you"; on quiet noise: "Thank you for watching.").
# As a whole transcript, in a conversation that is not in English, none of these was said.
_SILENCE_FILLERS = frozenset({
    "you", "thank you", "thanks", "thank you very much", "bye", "bye bye", "okay", "ok", "so", "oh", "the", "i", "uh", "um", "hmm",
})


@dataclass
class SanityResult:
    text: str        # what to use: the transcript, its repaired form, or "" for nothing said
    verdict: str     # "ok" | "fixed" | "garbage"
    by: str          # "code" | "model"
    reason: str = ""


def _looping(words: list[str]) -> bool:
    """The whole transcript is one word or short phrase said over and over."""
    for size in (1, 2, 3):
        if len(words) < size * _LOOP_MIN_REPEATS:
            continue
        unit = words[:size]
        if all(words[i:i + size] == unit for i in range(0, len(words) - len(words) % size, size)) and len(words) % size == 0:
            return True
    return False


def _wrong_script(text: str, language: str | None) -> bool:
    """Mostly letters of a script the conversation is not in (CJK or Arabic in a Russian
    or English conversation). Only the two languages this can be told for cheaply."""
    if language not in ("Russian", "English"):
        return False
    letters = _LETTERS.findall(text)
    if len(letters) < 4:
        return False
    known = sum(1 for c in letters if c.isascii() or "Ѐ" <= c <= "ӿ")
    return known / len(letters) < 0.5


def check_by_code(text: str, language: str | None = None) -> SanityResult | None:
    """Step 1. A verdict when code alone can give one, else None."""
    stripped = text.strip()
    if not stripped or not _LETTERS.search(stripped):
        return SanityResult("", "garbage", "code", "no words")
    if _HALLUCINATIONS.match(stripped):
        return SanityResult("", "garbage", "code", "recognizer's invention on silence")
    words = re.findall(r"\w+", stripped.lower().replace("ё", "е"))
    if language is not None and language != "English" and " ".join(words) in _SILENCE_FILLERS:
        return SanityResult("", "garbage", "code", "recognizer's filler for silence")
    if _looping(words):
        return SanityResult("", "garbage", "code", "one phrase looping")
    if _wrong_script(stripped, language):
        return SanityResult("", "garbage", "code", "not the conversation's script")
    if len(words) < STT_SANITY_MODEL_MIN_WORDS:
        return SanityResult(stripped, "ok", "code", "short")
    return None


def _prompt(text: str, recent_dialogue_lines: list[str], language: str | None) -> str:
    dialogue = "\n".join(recent_dialogue_lines[-4:]) or "(nothing yet)"
    return (
        "CRITICAL: a program parses your reply. Reply with exactly one of: OK | FIX: <the corrected text> | GARBAGE\n\n"
        f"Below is what a speech recognizer heard a user say to their voice assistant, in {language or 'their own language'}. "
        "Judge only whether it is a meaningful, connected utterance, given the conversation so far.\n"
        "- OK: it makes sense as it is. Casual speech, filler words, slang, an unfinished thought, a name or a term "
        "you do not know, a question you cannot answer -- all of that is OK. When in doubt, OK.\n"
        "- FIX: it makes sense except for a word or two that are plainly a mishearing of something that sounds "
        "alike and fits (\"по лохе это должно работать\" -> \"по логике это должно работать\"). Give the whole "
        "utterance back with only those words replaced. Never rephrase, complete, shorten, translate or "
        "answer it; never touch names, numbers or wording that already makes sense.\n"
        "- GARBAGE: it is not speech addressed to anyone -- words with no connection to each other, the "
        "recognizer's own filler, text in another language than the conversation with no sense in it.\n\n"
        f"Conversation so far:\n---\n{dialogue}\n---\n\n"
        f"Heard:\n---\n{text}\n---\n\n"
        "Reply now."
    )


def _accept_fix(heard: str, fixed: str) -> bool:
    """Step 3: the repair stays a repair."""
    a, b = heard.lower().replace("ё", "е"), fixed.lower().replace("ё", "е")
    if not fixed.strip() or abs(len(fixed) - len(heard)) > max(8, len(heard) * 0.3):
        return False
    # Nothing numeric may appear that was not heard: a digit is never a mishearing repair.
    if set(re.findall(r"\d+", b)) - set(re.findall(r"\d+", a)):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= STT_SANITY_FIX_MIN_SIMILARITY


async def check_transcript(
    text: str, recent_dialogue_lines: list[str] | None = None, language: str | None = None, resolve=None,
) -> SanityResult:
    """The whole pass. `resolve(prompt) -> raw reply` is the model step; without it
    (or on any failure of it) the transcript passes as recognition gave it. Never raises."""
    by_code = check_by_code(text, language)
    if by_code is not None:
        if by_code.verdict != "ok":
            log_event("engine", "stt_sanity", verdict=by_code.verdict, by="code", reason=by_code.reason, heard=text[:200])
        return by_code
    heard = text.strip()
    if resolve is None:
        return SanityResult(heard, "ok", "code", "no model step")
    started = time.monotonic()
    try:
        raw = str(await asyncio.wait_for(resolve(_prompt(heard, recent_dialogue_lines or [], language)), timeout=STT_SANITY_TIMEOUT_S) or "").strip()
    except Exception as exc:  # noqa: BLE001 -- a real utterance is never lost to this pass
        log_event("engine", "stt_sanity_failed", error=str(exc)[:200], error_type=type(exc).__name__,
                  ms=int((time.monotonic() - started) * 1000))
        return SanityResult(heard, "ok", "code", "model step failed")
    ms = int((time.monotonic() - started) * 1000)
    upper = raw.upper()
    if upper.startswith("GARBAGE"):
        log_event("engine", "stt_sanity", verdict="garbage", by="model", ms=ms, heard=heard[:200])
        return SanityResult("", "garbage", "model")
    if upper.startswith("FIX"):
        fixed = raw.split(":", 1)[1].strip().strip("\"«»") if ":" in raw else ""
        if fixed and fixed != heard and _accept_fix(heard, fixed):
            log_event("engine", "stt_sanity", verdict="fixed", by="model", ms=ms, heard=heard[:200], fixed=fixed[:200])
            return SanityResult(fixed, "fixed", "model")
        log_event("engine", "stt_sanity", verdict="ok", by="model", ms=ms, reason="fix rejected", heard=heard[:200], fixed=fixed[:200])
        return SanityResult(heard, "ok", "model", "fix rejected")
    log_event("engine", "stt_sanity", verdict="ok", by="model", ms=ms)
    return SanityResult(heard, "ok", "model")

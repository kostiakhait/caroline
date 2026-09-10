"""Ports backend/src/voice.ts -- TTS/STT/language-detection/cheap-image-
description via Camerlengo, plus the local edge-tts fallback path for TTS.
Used by app/main.py's tts/stt control_request ops and by
app_browser_plugin.py's app_browser_describe.
"""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from app.logging_setup import log_event
from app.plugins.sw_api import CAROLINE_SW_KEY, _post_json

LOCAL_TTS_PORT = 9414


def local_tts_url() -> str:
    return f"http://127.0.0.1:{LOCAL_TTS_PORT}/tts"


class VoiceApiError(Exception):
    pass


async def transcribe_audio(audio_base64: str, fmt: str, session: str | None = None) -> str:
    body: dict[str, Any] = {"command": "ai:stt", "key": CAROLINE_SW_KEY, "audio": audio_base64, "format": fmt}
    if session:
        body["session"] = session
    data = await _post_json(body)
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        if data.get(".errcode") == "402":
            raise VoiceApiError("insufficient_balance")
        raise VoiceApiError(str(data.get(".reason") or "STT failed"))
    return data["result"]


def voice_for_gender(gender: str) -> str:
    """Picks a voice matching the persona's gender field -- free text
    (English or Russian), so this matches on substrings rather than an
    exact enum. Defaults to the female voice for anything ambiguous."""
    g = gender.strip().lower()
    if g.startswith("male") or g.startswith("муж"):
        return "Onyx"
    return "Nova"


# Edge-tts (Microsoft's per-locale Neural voices) needs a specific voice
# per language -- unlike Camerlengo's Nova/Onyx, which are multilingual on
# their own and need no such table.
_EDGE_TTS_VOICES = {
    "en": {"female": "en-US-AriaNeural", "male": "en-US-GuyNeural"},
    "ru": {"female": "ru-RU-SvetlanaNeural", "male": "ru-RU-DmitryNeural"},
}


def _edge_tts_voice_for(camero_voice: str, language: str | None) -> str:
    table = _EDGE_TTS_VOICES.get(language or "", _EDGE_TTS_VOICES["en"])
    return table["male"] if camero_voice == "Onyx" else table["female"]


async def detect_language(text: str) -> str | None:
    """Real LLM-backed language detection (Camerlengo's ai:detectLanguage)
    -- deliberately NOT a Cyrillic/Latin heuristic. Returns a lowercase
    ISO 639-1 code, or None if the API call fails or genuinely can't tell.
    NOTE: this is the OLD, single-shot detector -- only used here to pick
    an edge-tts VOICE (low-stakes). The separate, higher-stakes "detect
    the user's own language for system-prompt steering" call site is
    intentionally NOT this function -- see the dedicated
    resolve-based-language-detection redesign plan for that one."""
    try:
        data = await _post_json({"command": "ai:detectLanguage", "key": CAROLINE_SW_KEY, "text": text})
    except Exception:
        return None
    if data.get(".status") != "ok" or not isinstance(data.get("language"), str):
        return None
    iso = data["language"].strip().lower()
    return iso if iso and iso != "unknown" else None


async def resolve_user_language(recent_text: str, session: str | None = None) -> str | None:
    """Redesign (2026-09-09, see the resolve-based-language-detection plan):
    determines what language the USER (not Caroline) is writing in, via
    ai:resolve (the same general-purpose command clean_text_for_speech
    already uses) rather than ai:detectLanguage -- returns the language's
    own English name (e.g. "Russian", "Spanish"), not an ISO code, and is
    NOT collapsed to a ru/en binary. Deliberately no artificial local
    timeout here -- only ever called fire-and-forget from chat_session.py's
    refresh_language_in_background, never awaited synchronously in a
    user-facing path."""
    prompt = (
        "Determine what language the USER is writing in, based on their most recent messages below (ignore any "
        "assistant/system text mixed in -- focus only on the user's own words). Reply with ONLY the language's "
        'English name (e.g. "Russian", "Spanish", "English") and nothing else -- no punctuation, no explanation.\n\n'
        f"{recent_text}"
    )
    body: dict[str, Any] = {"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": prompt}
    if session:
        body["session"] = session
    try:
        data = await _post_json(body)
    except Exception as exc:
        log_event("plugin:voice", "resolve_user_language_request_failed", error=str(exc))
        return None
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        log_event("plugin:voice", "resolve_user_language_bad_response", status=data.get(".status"), reason=data.get(".reason"))
        return None
    name = data["result"].strip()
    return name or None


async def generate_progress_comment(
    user_question: str, current_activity: str | None, language: str, session: str | None = None,
) -> str | None:
    """Per explicit instruction (2026-09-10): Caroline has no way to
    interrupt her own main session mid-turn just to narrate progress
    without genuinely disrupting whatever she's doing (the SDK only
    delivers a new injected message once the CURRENT turn -- the whole
    tool-call chain -- has fully finished; interrupt() is a real abort,
    not a "pause and continue" signal). This sidesteps that entirely: a
    SEPARATE, lightweight ai:resolve call (model SMALL, per explicit
    instruction -- this is filler narration, not worth full price)
    drafts a short in-character remark on her behalf, sent straight to
    the client as its own chat message. The real session never sees or
    knows about this -- it's a cosmetic stand-in for "I'm still working
    on it", not something she said or will remember. Returns None on any
    failure (network, bad response, SW unavailable) -- always a silent
    skip, never surfaced as an error to the user."""
    activity_line = f'She is currently in the middle of: {current_activity}. ' if current_activity else ""
    prompt = (
        "You are drafting ONE short, natural, in-character placeholder remark on behalf of an AI assistant "
        "who is silently in the middle of a longer task and hasn't said anything to the user in over a "
        "minute -- this is NOT her speaking directly, you're standing in for her so the user knows she's "
        f'still working. The user originally asked: "{user_question}". {activity_line}'
        "Write ONE short, casual sentence (two at most), in first person, connecting what she's doing back "
        "to the user's original request -- no technical or internal details (tool names, file paths, "
        f"code, session/system mechanics). Reply in {language}. Reply with ONLY that sentence, nothing else."
    )
    body: dict[str, Any] = {"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": prompt, "model": "SMALL"}
    if session:
        body["session"] = session
    try:
        data = await _post_json(body)
    except Exception as exc:
        log_event("plugin:voice", "generate_progress_comment_request_failed", error=str(exc))
        return None
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        log_event("plugin:voice", "generate_progress_comment_bad_response", status=data.get(".status"), reason=data.get(".reason"))
        return None
    text = data["result"].strip()
    return text or None


async def _synthesize_speech_locally(text: str, voice: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                res = await client.post(local_tts_url(), json={"text": text, "voice": voice})
                break
            except httpx.TransportError as exc:
                last_err = exc
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise last_err  # type: ignore[misc]
        if res.status_code >= 400:
            raise VoiceApiError(f"local TTS server returned {res.status_code}: {res.text[:300]}")
        return base64.b64encode(res.content).decode("ascii")


async def _synthesize_speech_via_camerlengo(text: str, voice: str, session: str | None = None) -> str:
    body: dict[str, Any] = {"command": "ai:tts", "key": CAROLINE_SW_KEY, "text": text, "voice": voice}
    if session:
        body["session"] = session
    data = await _post_json(body)
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        if data.get(".errcode") == "402":
            raise VoiceApiError("insufficient_balance")
        raise VoiceApiError(str(data.get(".reason") or "TTS failed"))
    return data["result"]  # base64 MP3


async def synthesize_speech(text: str, voice: str = "Nova", session: str | None = None) -> str:
    """Prefers the local edge-tts server over Camerlengo's ai:tts -- purely
    a latency optimization (no Camerlengo round trip), tried FIRST
    regardless of session, falling back to Camerlengo whenever the local
    attempt fails for any reason (never a hard new dependency)."""
    language = await detect_language(text)
    edge_voice = _edge_tts_voice_for(voice, language)
    try:
        audio = await _synthesize_speech_locally(text, edge_voice)
        log_event("plugin:voice", "tts_local_ok", edge_voice=edge_voice, text_len=len(text))
        return audio
    except Exception as exc:
        log_event("plugin:voice", "tts_local_failed_falling_back", edge_voice=edge_voice, error=str(exc))
        return await _synthesize_speech_via_camerlengo(text, voice, session)


# Skip the LLM cleanup call entirely when the text plainly has nothing for
# it to fix -- no markdown/HTML syntax, no digits.
_NEEDS_TTS_CLEANUP = re.compile(r"[*_`#|~]|<[a-z][^>]*>|\d", re.IGNORECASE)


async def clean_text_for_speech(text: str, session: str | None = None) -> str:
    """Strips markdown/HTML formatting and rewrites digits/numerals into
    their spoken, correctly-inflected form before text reaches TTS/Visual
    Mode."""
    if not _NEEDS_TTS_CLEANUP.search(text):
        return text
    prompt = (
        "Rewrite the following text so it is ready to be read aloud by a text-to-speech engine. "
        "Perform exactly these transformations, nothing else:\n"
        "- Strip ALL markdown formatting (**bold**, *italic*, `code`, # headers, - / * bullet "
        "lists, [links](url), | tables |, --- rules, > quotes) down to plain spoken prose -- "
        "keep the words, drop the syntax.\n"
        "- Strip ALL HTML tags the same way -- keep the text content, drop the markup.\n"
        "- Rewrite every digit/numeral (dates, quantities, times, ordinals, etc.) as the words "
        "a native speaker would actually SAY aloud, correctly inflected/declined for its exact "
        "grammatical role in the sentence (case, number, gender, as the language requires) -- "
        'not just the bare nominative/cardinal form. Example in Russian: "5 книг" -> "пяти книг", '
        '"с 5 книгами" -> "с пятью книгами", "2024 год" -> "две тысячи двадцать четвёртый год".\n'
        "- Keep the exact same language and meaning. Do not translate, summarize, add, or remove "
        "any information. Do not add commentary, explanations, or quotation marks around the result.\n"
        "- Return ONLY the rewritten text, nothing else.\n\n"
        f"Text:\n{text}"
    )
    body: dict[str, Any] = {"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": prompt}
    if session:
        body["session"] = session
    try:
        data = await _post_json(body)
    except Exception:
        return text
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        return text
    return data["result"]


async def describe_image_cheap(base64_png: str) -> dict[str, Any]:
    """Cheap image understanding (Camerlengo's ai:describeImage, a small
    model) as a deliberately CHEAPER alternative to putting raw image
    bytes into Claude's own context."""
    data = await _post_json({"command": "ai:describeImage", "key": CAROLINE_SW_KEY, "content": base64_png, "type": "base64"})
    if data.get(".status") != "ok" or not isinstance(data.get("result"), dict):
        raise VoiceApiError(str(data.get(".reason") or "describeImage failed"))
    r = data["result"]
    return {"description": str(r.get("description") or ""), "objects": r.get("objects"), "palette": r.get("palette")}

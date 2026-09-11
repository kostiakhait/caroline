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


# Guards against the failure modes seen live rather than trusting any
# non-empty .result blindly (screenshots, tab 2):
#  - the SMALL model REFUSING the task ("I'm sorry, I can't help with
#    that.") and that refusal getting shown as a Caroline chat bubble;
#  - a raw API/catalog-lookup error leaking straight through (the "didn't
#    find a matching entry ... in the available catalog" incident);
#  - a reply in the wrong SCRIPT entirely (Chinese "正在打开文档。" for a
#    Russian conversation);
#  - raw JSON, or a reply far longer than "one short sentence".
# Any of these -> treat as no usable response, show nothing.
_NARRATION_GARBAGE_PATTERNS = [
    re.compile(r"\bi(?:'m| am)\s+sorry\b", re.IGNORECASE),
    re.compile(r"\bi\s+(?:can(?:'t|not)|won'?t|am unable to|cannot)\s+(?:help|assist|do that|provide|comply)", re.IGNORECASE),
    re.compile(r"\bas an? (?:ai|language model)\b", re.IGNORECASE),
    re.compile(r"\bне могу (?:помочь|это сделать|выполнить)\b", re.IGNORECASE),
    re.compile(r"\bизвини(?:те)?[,.\s].{0,40}\bне могу\b", re.IGNORECASE),
    re.compile(r"\bкак (?:ИИ|языковая модель)\b", re.IGNORECASE),
    re.compile(r"\bmatching entry\b", re.IGNORECASE),
    re.compile(r"\bavailable catalog\b", re.IGNORECASE),
    re.compile(r"\berrcode\b", re.IGNORECASE),
    re.compile(r"^\s*[{\[]"),  # raw JSON/array leaking through
]
_NARRATION_MAX_CHARS = 400
# Han / Hiragana / Katakana / Hangul. Progress narration for this product
# is only ever asked for in a European language; a CJK reply is the SMALL
# model flailing, never legitimate here.
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def _looks_like_narration_garbage(text: str, language: str = "") -> bool:
    if len(text) > _NARRATION_MAX_CHARS:
        return True
    if _CJK_RE.search(text) and not re.search(r"chin|japan|korea|mandarin|中文", language, re.IGNORECASE):
        return True
    return any(p.search(text) for p in _NARRATION_GARBAGE_PATTERNS)


async def generate_progress_comment(recent_dialogue: str, language: str, session: str | None = None) -> str | None:
    """Per explicit instruction (2026-09-10): Caroline has no way to
    interrupt her own main session mid-turn just to narrate progress
    without genuinely disrupting whatever she's doing (the SDK only
    delivers a new injected message once the CURRENT turn -- the whole
    tool-call chain -- has fully finished; interrupt() is a real abort,
    not a "pause and continue" signal). This sidesteps that entirely: a
    SEPARATE, lightweight ai:resolve call (model SMALL, per explicit
    instruction -- this is filler narration, not worth full price)
    drafts a short in-character remark on her behalf, sent straight to
    the client as its own chat message.

    Reverted (2026-09-11) back to "SMALL" -- the temporary openai/gpt5-nano
    bypass (added when Camerlengo's SMALL category resolved to a 403ing
    model) is no longer needed: live-tested against SMALL directly (a
    narrator-shaped prompt, an arithmetic question, and a trick question)
    and got coherent, correctly-languaged, non-garbage answers, confirming
    the category now points at a working model again.

    The real session never sees or knows about this -- it's a cosmetic
    stand-in for "I'm still working on it", not something she said or
    will remember.

    Redesigned (2026-09-10) after live evidence (screenshots) that the
    original single-question + tool-names-as-"activity" version produced
    bland, repetitive, OPERATION-sounding remarks ("I'm checking the
    phone-related messages...", "let me pull up that operation...")
    instead of something addressed to the user that actually engages with
    what's being discussed. Now takes the real recent back-and-forth
    (chat_session.py's _gather_recent_dialogue_for_narration) instead of
    a single frozen question, so each call has fresh material to react to
    and the model can speak to the actual substance instead of describing
    internal mechanics. Returns None on any failure (network, bad
    response, SW unavailable, or a response that fails the garbage check
    below) -- always a silent skip, never surfaced as an error."""
    prompt = (
        "You are standing in, for a moment, on behalf of an AI assistant who is mid-conversation with a "
        "specific person and has been quietly working on their last message for over a minute now without "
        "saying anything back yet. Draft ONE short remark in her voice to keep the conversation feeling "
        "alive. This is NOT a status update about internal work -- never say things like \"I'm checking/"
        "pulling up/sorting through/looking into/working on X\", never mention tools, files, operations, or "
        "how long anything is taking. Instead, react like someone genuinely engaged with the actual topic "
        "would: add a real, specific thought connected to what's being discussed -- a relevant detail, a "
        "follow-up angle, a small observation -- not a generic placeholder that could fit any conversation.\n\n"
        "Bug fix (2026-09-10), read carefully -- confirmed live this remark once said \"I'll create a GitLab "
        "repo and send you the link\" while the real, actual work happening at that exact moment was something "
        "else entirely (an unrelated code search), and the user took it as a real promise that then never got "
        "fulfilled. You are NOT the real assistant and have no idea what she is actually doing right now -- "
        "NEVER commit to a new action on her behalf (no \"I'll do X\", \"I will send/create/check Y\", no new "
        "promises or plans of any kind, however small). Only react to what's ALREADY in the conversation below "
        "-- an observation, a reaction, a connection to something already said -- never something forward-"
        "looking that could turn out to be false.\n\n"
        f"Here is the real recent conversation between her and the user (oldest first):\n---\n{recent_dialogue}\n---\n\n"
        "Write ONE short, natural sentence (two at most), in first person, speaking directly to the user. "
        "Reply in whatever language the USER's OWN lines (marked \"User:\") above are written in -- ignore "
        "what language Caroline's own lines happen to use, even if they dominate the text (e.g. she may be "
        "quoting or analyzing English-language technical/legal material mid-conversation while the user "
        "themselves is writing in a different language entirely -- go by the user's words, not the topic's). "
        f"Only if there are no \"User:\" lines at all above, default to {language}. Reply with ONLY that "
        "sentence, nothing else -- no quotes, no preamble."
    )
    # Bug fix (2026-09-11), per explicit instruction: reverted to "SMALL"
    # (the openai/gpt5-nano bypass above is no longer needed -- see this
    # function's own docstring).
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
    if not text:
        return None
    if _looks_like_narration_garbage(text, language):
        log_event("plugin:voice", "generate_progress_comment_rejected_garbage", text=text[:300])
        return None
    log_event("plugin:voice", "generate_progress_comment_ok", dialogue_chars=len(recent_dialogue), text=text)
    return text


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

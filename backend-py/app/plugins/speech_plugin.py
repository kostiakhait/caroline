"""speech -- speech_to_text / text_to_speech: speech recognition and
synthesis as plain tools for the model, through SquirrelWisdom (Camerlengo's
ai:stt / ai:tts).

These work on FILES, for the model's own tasks: transcribe a recording the
user handed over or a voice message that arrived, produce an audio file to
attach or send. They are deliberately separate from the two voice paths
Caroline already has, and change neither:
  - the microphone button (voice input) transcribes what the user says to
    her -- main.py, voice_api.transcribe_audio;
  - speak_text (voice_playback_plugin.py) plays a reply aloud in the chat
    window, preferring the local edge-tts voice.
text_to_speech here always goes through SquirrelWisdom and always produces a
file; nothing is played.

Both tools need the user's SquirrelWisdom account and are charged to its
wallet by the server (speech recognition by the size of the audio, synthesis
by the length of the text).
"""

from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.plugins.notes_api import SessionManager
from app.plugins.sw_api import CAROLINE_SW_KEY, SessionExpiredError, _post_json
from app.session_context import get_send
from app.sw_gate import require_sw_or_prompt
from app.workspace_dir import WORKSPACE_DIR

_sessions = SessionManager()

MAX_AUDIO_BYTES = 25 * 1024 * 1024      # one request; longer recordings must be cut into parts first
MAX_TEXT_CHARS = 20000                  # one request
REQUEST_TIMEOUT_S = 300.0               # recognition of a long recording takes a while
DEFAULT_VOICE = "Nova"
VOICES = ("Nova", "Onyx", "Alloy")      # Nova: female; Onyx: male; Alloy: neutral
AUDIO_FORMATS = ("wav", "mp3", "m4a", "ogg", "oga", "opus", "webm", "flac", "aac", "mp4", "mpeg", "mpga")
OUTPUT_DIR = "speech"                   # under the workspace, when the model names no file


_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}$")


class SpeechError(Exception):
    """Something the model can act on; the message is for the model."""


async def _call(body: dict[str, Any]) -> str:
    """One ai:stt / ai:tts call for the logged-in user; returns "result"."""
    async def once(session: str) -> str:
        data = await _post_json({**body, "key": CAROLINE_SW_KEY, "session": session}, timeout=REQUEST_TIMEOUT_S)
        if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
            reason = str(data.get(".reason") or "the service returned no result")
            if data.get(".errcode") == "402":
                raise SpeechError("the user's SquirrelWisdom balance is too low; tell them to top up, do not retry")
            if "session" in reason.lower():
                raise SessionExpiredError(reason)
            raise SpeechError(reason)
        return data["result"]
    return await _sessions.with_session(once)


async def _gate() -> dict[str, Any] | None:
    gate = await require_sw_or_prompt(get_send())
    return None if gate.ok else {"text": gate.message, "is_error": True}


async def speech_to_text(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    path = Path(str(args.get("path") or "")).expanduser()
    if not path.is_file():
        return {"text": f"There is no audio file at {args.get('path')}.", "is_error": True}
    fmt = (str(args.get("format") or "") or path.suffix.lstrip(".")).lower()
    if fmt not in AUDIO_FORMATS:
        return {"text": f'Unsupported audio format "{fmt}". Supported: {", ".join(AUDIO_FORMATS)}.', "is_error": True}
    size = path.stat().st_size
    if size == 0:
        return {"text": f"{path} is empty.", "is_error": True}
    if size > MAX_AUDIO_BYTES:
        return {"text": f"{path} is {size} bytes; one request takes at most {MAX_AUDIO_BYTES}. Cut the recording into parts and transcribe each.", "is_error": True}
    body: dict[str, Any] = {"command": "ai:stt", "audio": base64.b64encode(path.read_bytes()).decode("ascii"), "format": fmt}
    # The recognizer takes a language CODE ("ru", "en") and rejects the whole
    # request for anything else (confirmed live: "Russian" -> provider 400).
    # Anything that is not a code is left out: the language is then detected.
    language = str(args.get("language") or "").strip().lower()
    if _LANGUAGE_CODE.match(language):
        body["language"] = language
    if args.get("hint"):
        body["prompt"] = str(args["hint"])
    try:
        text = await _call(body)
    except SpeechError as exc:
        return {"text": f"Speech recognition failed: {exc}", "is_error": True}
    log_event("plugin:speech", "speech_to_text", bytes=size, format=fmt, text_len=len(text))
    return {"text": text if text.strip() else "(no speech was recognized in this recording)"}


async def text_to_speech(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    text = str(args.get("text") or "").strip()
    if not text:
        return {"text": 'text_to_speech needs "text".', "is_error": True}
    if len(text) > MAX_TEXT_CHARS:
        return {"text": f"The text is {len(text)} characters; one request takes at most {MAX_TEXT_CHARS}. Split it and make a file per part.", "is_error": True}
    voice = str(args.get("voice") or DEFAULT_VOICE)
    if voice not in VOICES:
        return {"text": f'Unknown voice "{voice}". Available: {", ".join(VOICES)}.', "is_error": True}
    if args.get("save_to"):
        target = Path(str(args["save_to"])).expanduser()
        if target.suffix.lower() != ".mp3":
            return {"text": 'The audio is MP3: "save_to" must end in .mp3.', "is_error": True}
    else:
        target = Path(WORKSPACE_DIR) / OUTPUT_DIR / f"speech-{uuid.uuid4().hex[:12]}.mp3"
    body: dict[str, Any] = {"command": "ai:tts", "text": text, "voice": voice}
    if args.get("language"):
        body["language"] = str(args["language"])
    try:
        audio_b64 = await _call(body)
    except SpeechError as exc:
        return {"text": f"Speech synthesis failed: {exc}", "is_error": True}
    audio = base64.b64decode(audio_b64)
    if not audio:
        return {"text": "Speech synthesis returned no audio.", "is_error": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(audio)
    log_event("plugin:speech", "text_to_speech", text_len=len(text), voice=voice, bytes=len(audio))
    return {"text": f"Saved {len(audio)} bytes of MP3 to {target}"}


_USAGE_INSTRUCTIONS = (
    "speech_to_text(path) transcribes an audio FILE -- a recording the user gave you, a voice message that "
    "arrived, the sound track of something you downloaded -- and returns its text. Pass \"language\" as a "
    "two-letter code (\"ru\", \"en\") when you know it -- otherwise it is detected -- and \"hint\" with "
    "names or terms likely to occur, to help recognition. "
    f"Formats: {', '.join(AUDIO_FORMATS)}; up to {MAX_AUDIO_BYTES // (1024 * 1024)} MB per call -- cut a longer "
    "recording into parts.\n"
    "text_to_speech(text) makes an MP3 FILE of the text read aloud and returns its path -- for a file to attach, "
    f"send or keep. Voices: {', '.join(VOICES)} (Nova is female, Onyx male). Pass \"save_to\" (ending in .mp3) "
    "to choose where it goes, and \"language\" when you know it. It plays nothing: to SAY something to the user "
    "in the chat window use speak_text instead.\n"
    "Both go through the user's SquirrelWisdom account and cost money from its wallet (recognition by the size "
    "of the audio, synthesis by the length of the text), so do not run them on things nobody asked about."
)


PLUGIN = Plugin(
    name="speech",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool(
            "speech_to_text",
            "Transcribes an audio file (a recording, a voice message) into text. \"path\" is the local file; "
            "optional \"language\" (a two-letter code: \"ru\", \"en\") and \"hint\" (names/terms likely to occur) "
            "help recognition.",
            {"path": str, "language": str | None, "hint": str | None, "format": str | None},
            speech_to_text,
        ),
        PluginTool(
            "text_to_speech",
            "Makes an MP3 file of the given text read aloud and returns its path -- a file to attach, send or "
            "keep. It plays nothing: to speak to the user in the chat use speak_text.",
            {"text": str, "voice": str | None, "language": str | None, "save_to": str | None},
            text_to_speech,
        ),
    ],
)

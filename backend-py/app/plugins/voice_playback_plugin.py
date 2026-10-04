"""speak_text -- on-demand voice playback (and, with animated=true, the
talking-head Visual Mode animation) for text the model chooses to say aloud,
independent of whether the user spoke to Caroline by voice.

Synthesis reuses voice_api.synthesize_speech (in-process edge-tts, Camerlengo
fallback). The audio goes to the page as a play_speech event; chat.js queues
it on the same speech queue every other reply uses.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.plugins.voice_api import clean_text_for_speech, synthesize_speech
from app.session_context import get_send


async def speak_text(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("text must be a non-empty string")
    animated = bool(args.get("animated"))
    spoken = await clean_text_for_speech(text)
    audio_b64 = await synthesize_speech(spoken, voice=str(args.get("voice") or "Nova"))
    if not audio_b64:
        raise RuntimeError("speech synthesis returned no audio")
    send = get_send()
    await send({"type": "play_speech", "text": spoken, "animated": animated, "audioBase64": audio_b64})
    log_event("plugin:voice-playback", "speak_text_sent", text_len=len(spoken), animated=animated)
    return {"text": "Playing it now" + (" with the animated talking-head." if animated else ".")}


PLUGIN = Plugin(
    name="voice-playback",
    usage_instructions=(
        "Use speak_text when the user asks you to say something out loud, read a passage aloud, or play a spoken "
        "message -- it plays immediately, whether or not they spoke to you by voice. Pass animated=true only when "
        "they explicitly want the animated talking-head video, not for ordinary spoken replies."
    ),
    tools=[
        PluginTool(
            "speak_text",
            "Plays the given text aloud through Caroline's voice. With animated=true, plays it as the animated "
            "talking-head video instead of plain audio.",
            {"text": str, "voice": str | None, "animated": bool | None},
            speak_text,
        ),
    ],
)

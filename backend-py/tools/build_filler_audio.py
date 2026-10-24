"""Synthesizes the shipped filler remarks (ru, en; female and male voice) into
app/assets/fillers/<lang>/<gender>/ -- see app/filler_phrases.py. Run from backend-py, needs
network access to the speech service (edge-tts):

    python tools/build_filler_audio.py [output_dir]

The installer ships the result; any other language is built at its first use.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import filler_phrases  # noqa: E402
from app.plugins.voice_api import _EDGE_TTS_VOICES, _synthesize_speech_locally  # noqa: E402


async def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else filler_phrases.SHIPPED_DIR
    for language in ("ru", "en"):
        for gender in ("female", "male"):
            voice = _EDGE_TTS_VOICES[language][gender]

            async def synthesize(text: str, _language: str, voice: str = voice) -> str:
                return await _synthesize_speech_locally(text, voice)

            async def no_model(_prompt: str) -> str:
                raise RuntimeError("built-in sets need no translation")

            await filler_phrases.build_set(language, gender, out, synthesize, no_model)
            print(language, gender, voice, "done", flush=True)


asyncio.run(main())

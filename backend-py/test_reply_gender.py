"""reply.gender: the words that refer to the speaker in the persona's gender; only word endings may change (2026-11-27).
Run: python -m pytest test_reply_gender.py -q"""

import asyncio

from app.microagents import reply_gender
from app.plugins import voice_api


def test_endings_only():
    assert voice_api.only_endings_changed("Я заменил траву и деревья.", "Я заменила траву и деревья.")
    assert voice_api.only_endings_changed("Я рад, что всё готов", "Я рада, что всё готова")
    assert not voice_api.only_endings_changed("Я заменил траву.", "Я заменила траву и деревья.")
    assert not voice_api.only_endings_changed("Я заменил траву.", "Я выбросила траву.")


def test_prompt_and_verify():
    p = reply_gender.prompt_gender({"text": "Я сделал.", "gender": "female"}, None)
    assert "female" in p and "Я сделал." in p
    assert reply_gender.verify_gender({}, {"text": ""}) is not None
    assert reply_gender.verify_gender({}, {"text": "Я сделала."}) is None


def test_unknown_gender_or_empty_text_is_untouched():
    assert asyncio.run(voice_api.fix_gender("Я сделал.", None)) == "Я сделал."
    assert asyncio.run(voice_api.fix_gender("  ", "female")) == "  "

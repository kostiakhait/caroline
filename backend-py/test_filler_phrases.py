"""Filler remarks (app/filler_phrases.py; the module had no test, 2026-11-22): the shipped audio matches the
phrases in code, no repeats, a translated set, the gentle re-asks of the synthesis, one build at a time.
Run: python -m pytest test_filler_phrases.py -q"""

import asyncio
import base64
import json

import pytest

from app import filler_phrases as fp


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(fp, "_recent", [])
    monkeypatch.setattr(fp, "_building", {})


@pytest.mark.parametrize("language", sorted(fp.BUILT_IN))
@pytest.mark.parametrize("gender", ["female", "male"])
def test_the_shipped_audio_says_exactly_the_phrases_in_code(language, gender):
    """A phrase edited in code without rebuilding the audio would be said one way and shown/logged another."""
    directory = fp.SHIPPED_DIR / language / gender
    entries = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    assert [e["text"] for e in entries] == [fp.render_gender(t, gender) for t in fp.BUILT_IN[language]]
    for entry in entries:
        head = (directory / entry["file"]).read_bytes()[:3]
        assert head == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"), entry["file"]


def test_render_gender():
    assert fp.render_gender("Немного задумал{ась|ся}.", "female") == "Немного задумалась."
    assert fp.render_gender("Немного задумал{ась|ся}.", "male") == "Немного задумался."
    assert fp.render_gender("No marker here", "male") == "No marker here"


def write_set(root, language, gender, texts):
    directory = root / language / gender
    directory.mkdir(parents=True)
    entries = []
    for i, text in enumerate(texts):
        (directory / f"{i:03d}.mp3").write_bytes(b"ID3" + text.encode())
        entries.append({"text": text, "file": f"{i:03d}.mp3"})
    (directory / "index.json").write_text(json.dumps(entries), encoding="utf-8")


def test_no_filler_repeats_until_the_set_runs_out(tmp_path):
    write_set(tmp_path, "xx", "female", ["a", "b", "c"])
    said = [fp.pick_filler("xx", "female", tmp_path)[0] for _ in range(3)]
    assert sorted(said) == ["a", "b", "c"]                       # all three before any repeat
    text, audio = fp.pick_filler("xx", "female", tmp_path)       # then the whole set again
    assert base64.b64decode(audio) == b"ID3" + text.encode()
    assert fp.pick_filler("yy", "female", tmp_path) is None      # no set for that language yet


def test_translate_set_takes_only_a_full_list():
    n = len(fp.ENGLISH)

    async def good(prompt):
        assert '"de"' in prompt and "a woman" in prompt
        return "Here you go:\n" + json.dumps([f"Satz {i}" for i in range(n)])

    async def short(prompt):
        return json.dumps(["eins"])

    async def garbage(prompt):
        return "sorry"

    assert asyncio.run(fp.translate_set("de", "female", good)) == [f"Satz {i}" for i in range(n)]
    assert asyncio.run(fp.translate_set("de", "female", short)) is None
    assert asyncio.run(fp.translate_set("de", "female", garbage)) is None


def test_build_set_re_asks_what_failed_and_keeps_only_phrases_with_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "log_event", lambda *a, **k: None)
    n = len(fp.ENGLISH)
    attempts: dict[str, int] = {}

    async def resolve(prompt):
        return json.dumps([f"Satz {i}" for i in range(n)])

    async def synthesize(text, language):
        attempts[text] = attempts.get(text, 0) + 1
        number = int(text.split()[1])
        if number % 5 == 0 and attempts[text] == 1:
            raise RuntimeError("No audio was received")         # a burst refusal: fine on the gentler re-ask
        if number == 7:
            raise RuntimeError("always fails")
        return base64.b64encode(text.encode()).decode()

    asyncio.run(fp.build_set("de", "male", tmp_path, synthesize, resolve))
    entries = json.loads((tmp_path / "de" / "male" / "index.json").read_text(encoding="utf-8"))
    texts = [e["text"] for e in entries]
    assert len(texts) == n - 1 and "Satz 7" not in texts and "Satz 5" in texts
    assert attempts["Satz 5"] == 2 and attempts["Satz 7"] == 3 and attempts["Satz 1"] == 1
    assert not (tmp_path / "de" / "male" / "index.json.tmp").exists()
    assert fp.find_set("de", "male", tmp_path)[1] == entries


def test_ensure_set_builds_a_language_once_at_a_time(tmp_path, monkeypatch):
    started = []

    async def slow_build(language, gender, cache_root, synthesize, resolve):
        started.append(language)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(fp, "build_set", slow_build)

    async def go():
        fp.ensure_set("de", "female", tmp_path, None, None)
        fp.ensure_set("de", "female", tmp_path, None, None)
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert started == ["de"]
    fp.ensure_set("en", "female", tmp_path, None, None)          # shipped: nothing to build, no loop needed

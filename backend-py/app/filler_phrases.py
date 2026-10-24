"""Filler remarks for voice narration (2026-10-24, explicit instruction): short pre-coded
phrases ("Одну секунду", "Немного задумалась") that fill a pause nothing real can fill, so
the user never hears more than a few seconds of silence while Caroline works (see
docs/CONTINUOUS_NARRATION_PLAN.md).

Where the pieces come from:
  * texts -- Russian and English are written below. Grammatical gender of the persona is marked
    `{female|male}` ("Немного задумал{ась|ся}"). Any other language: a model translates the set
    once, at the first use (translate_set), and the result is kept on disk. No word lists in code
    for deciding anything: these are the phrases themselves.
  * audio -- ru and en are shipped synthesized in the installer (assets/fillers/<lang>/<gender>/,
    made by tools/build_filler_audio.py); any other language is synthesized at the first use,
    8 requests at a time, into the user's workspace. Until a language's set is ready,
    pick_filler returns None for it and nothing is played.

Layout of a set on disk (shipped or cached): <root>/<lang>/<female|male>/index.json
  [{"text": "...", "file": "001.mp3"}, ...]  plus the mp3 files next to it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
from pathlib import Path
from typing import Awaitable, Callable

from app.logging_setup import log_event

SYNTHESIS_PARALLELISM = 8
SHIPPED_DIR = Path(__file__).parent / "assets" / "fillers"

_MARKER = re.compile(r"\{([^{}|]*)\|([^{}|]*)\}")


def render_gender(template: str, gender: str) -> str:
    """'Немного задумал{ась|ся}.' -> the female or the male form. `gender`: 'female' or 'male'."""
    index = 1 if gender == "male" else 0
    return _MARKER.sub(lambda m: m.group(1 + index), template)


# --- the written sets -------------------------------------------------------------------------

RUSSIAN = [
    "Одну секунду.",
    "Сейчас продолжу.",
    "Немного задумал{ась|ся}.",
    "Мне нужно немного времени, чтобы сформулировать дальше.",
    "Секундочку.",
    "Одну минутку.",
    "Сейчас соберусь с мыслями.",
    "Так, смотрю дальше.",
    "Работаю над этим.",
    "Всё идёт, ещё немного.",
    "Почти готово, ещё чуть-чуть.",
    "Сейчас, сейчас.",
    "Подбираю слова.",
    "Проверяю ещё одну вещь.",
    "Ещё мгновение.",
    "Так, что у нас дальше.",
    "Сейчас посмотрю внимательнее.",
    "Дорабатываю.",
    "Разбираюсь, это требует времени.",
    "Вникаю в детали.",
    "Ещё секунду, пожалуйста.",
    "Продолжаю разбираться.",
    "Нужно ещё немного времени.",
    "Сверяю данные.",
    "Уточняю кое-что.",
    "Сейчас вернусь к этому.",
    "Так-так, минутку.",
    "Я здесь, просто работаю.",
    "Работа идёт, не пропадаю.",
    "Подумаю ещё чуть-чуть.",
    "Складываю картину целиком.",
    "Нужно ещё немного терпения.",
    "Сейчас буду готов{а|}.",
    "Сосредоточ{илась|ился} на этом.",
    "Немного замешкал{ась|ся}, продолжаю.",
    "Уже почти разобрал{ась|ся}.",
    "Секунду, перепроверю.",
    "Минуточку, дочитываю.",
    "Погружаюсь глубже.",
    "Сейчас всё станет понятнее.",
    "Ещё чуть-чуть, и скажу.",
    "Держу нить, секунду.",
    "Ищу нужное место.",
    "Тут есть над чем подумать.",
    "Я не молчу, я думаю.",
    "Выстраиваю порядок действий.",
    "Сейчас, дойду до сути.",
    "Одно мгновение, пожалуйста.",
    "Идём дальше, минутку.",
    "Присматриваюсь к деталям.",
    "Это не быстро, но я на месте.",
    "Так, сейчас сформулирую.",
    "Ещё немного, и расскажу.",
    "Сверяюсь с тем, что {нашла|нашёл}.",
    "Секунду, привожу всё в порядок.",
    "Проверяю, не упустил{а|} ли что-нибудь.",
    "Сейчас дособираю картину.",
    "Работаю, скоро будет ясно.",
    "Мне нужно ещё мгновение.",
    "Почти на месте.",
    "Сейчас закончу этот шаг.",
    "Ещё чуть-чуть терпения, пожалуйста.",
    "Всё под контролем, продолжаю.",
    "Минутку, завершаю.",
]

ENGLISH = [
    "One second.",
    "Let me continue in a moment.",
    "Just thinking for a moment.",
    "I need a little time to put the next part into words.",
    "Just a second.",
    "One moment, please.",
    "Let me collect my thoughts.",
    "Okay, looking further.",
    "Working on it.",
    "It's going, just a bit more.",
    "Almost there, a little longer.",
    "Hold on, hold on.",
    "Finding the right words.",
    "Checking one more thing.",
    "Just a moment more.",
    "So, what do we have next.",
    "Let me look at it more closely.",
    "Polishing it.",
    "Working through this, it takes a while.",
    "Getting into the details.",
    "One more second, please.",
    "Still working through it.",
    "I need a little more time.",
    "Cross-checking the data.",
    "Confirming something.",
    "I'll come back to that in a second.",
    "Hmm, one moment.",
    "I'm here, just working.",
    "Work is going on, I haven't gone anywhere.",
    "Let me think a little longer.",
    "Putting the whole picture together.",
    "A little more patience, please.",
    "I'll be ready shortly.",
    "Focusing on this.",
    "I hesitated a little, carrying on.",
    "Nearly worked it out.",
    "A second, double-checking.",
    "One moment, finishing reading.",
    "Digging deeper.",
    "It will all be clearer in a second.",
    "A little more and I'll tell you.",
    "Keeping hold of the thread, one second.",
    "Looking for the right place.",
    "There's something to think about here.",
    "I'm not silent, I'm thinking.",
    "Working out the order of steps.",
    "Getting to the point in a moment.",
    "Just one moment, please.",
    "Moving on, one minute.",
    "Looking closely at the details.",
    "This isn't quick, but I'm here.",
    "Right, formulating it now.",
    "A little more and I'll tell you everything.",
    "Checking against what I've found.",
    "One second, tidying it all up.",
    "Making sure I haven't missed anything.",
    "Just completing the picture.",
    "Working, it will be clear soon.",
    "Give me one more moment.",
    "Almost done here.",
    "Finishing this step now.",
    "Bear with me a little longer, please.",
    "Everything is under control, carrying on.",
    "One minute, wrapping up.",
]

BUILT_IN: dict[str, list[str]] = {"ru": RUSSIAN, "en": ENGLISH}

# --- sets on disk -----------------------------------------------------------------------------

_building: dict[tuple[str, str], asyncio.Task] = {}
_recent: list[str] = []
RECENT_MEMORY = 20


def _set_dir(root: Path, language: str, gender: str) -> Path:
    return root / language / gender


def _read_index(directory: Path) -> list[dict] | None:
    try:
        entries = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return entries if isinstance(entries, list) and entries else None


def find_set(language: str, gender: str, cache_root: Path) -> tuple[Path, list[dict]] | None:
    """The ready set for a language and gender: the shipped one, else the cached one."""
    for root in (SHIPPED_DIR, cache_root):
        directory = _set_dir(root, language, gender)
        entries = _read_index(directory)
        if entries:
            return directory, entries
    return None


def pick_filler(language: str, gender: str, cache_root: Path) -> tuple[str, str] | None:
    """(text, audio as base64) of a filler not said recently, or None when this language's set is
    not ready (and starts it building where there is anything to build, see ensure_set)."""
    found = find_set(language, gender, cache_root)
    if found is None:
        return None
    directory, entries = found
    fresh = [e for e in entries if e.get("text") not in _recent] or entries
    entry = random.choice(fresh)
    try:
        audio = base64.b64encode((directory / entry["file"]).read_bytes()).decode("ascii")
    except OSError:
        return None
    _recent.append(entry["text"])
    del _recent[:-RECENT_MEMORY]
    return entry["text"], audio


def forget_recent() -> None:
    _recent.clear()


async def translate_set(
    language: str, gender: str, resolve: Callable[[str], Awaitable[str]],
) -> list[str] | None:
    """The English set in another language, by a model, for a speaker of the given gender.
    None when the model's answer is not a list of the same length."""
    source = [render_gender(t, gender) for t in ENGLISH]
    prompt = (
        f"Translate each of these {len(source)} short spoken filler phrases into the language with ISO 639-1 "
        f"code \"{language}\". The speaker is {'a man' if gender == 'male' else 'a woman'}: use the "
        "grammatical gender of the speaker wherever the language has it. Natural, short, spoken; the same "
        "meaning and the same order. Reply with ONLY a JSON array of exactly "
        f"{len(source)} strings, no comments.\n\n{json.dumps(source, ensure_ascii=False)}"
    )
    raw = await resolve(prompt)
    start, end = raw.find("["), raw.rfind("]")
    try:
        translated = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    if not isinstance(translated, list) or len(translated) != len(source) or not all(isinstance(t, str) and t.strip() for t in translated):
        return None
    return [t.strip() for t in translated]


async def build_set(
    language: str, gender: str, cache_root: Path,
    synthesize: Callable[[str, str], Awaitable[str]], resolve: Callable[[str], Awaitable[str]],
) -> None:
    """Texts (translated when the language is not a built-in one) and their audio, 8 at a time,
    written into cache_root. `synthesize(text, language)` -> base64 mp3."""
    if language in BUILT_IN:
        texts = [render_gender(t, gender) for t in BUILT_IN[language]]
    else:
        texts = await translate_set(language, gender, resolve)
        if texts is None:
            log_event("engine", "filler_set_translation_failed", language=language, gender=gender)
            return
    gate = asyncio.Semaphore(SYNTHESIS_PARALLELISM)

    async def one(text: str) -> bytes | None:
        async with gate:
            try:
                return base64.b64decode(await synthesize(text, language))
            except Exception as exc:  # noqa: BLE001 -- a phrase that failed is simply left out
                log_event("engine", "filler_synthesis_failed", language=language, error=str(exc)[:160])
                return None

    audios = list(await asyncio.gather(*(one(t) for t in texts)))
    # The service answers a burst with "no audio" for some requests (measured: 12 of 64 at 8 at a
    # time): what failed is asked again, twice, gently.
    for gentler in (2, 1):
        missing = [i for i, audio in enumerate(audios) if not audio]
        if not missing:
            break
        gate = asyncio.Semaphore(gentler)
        for i, audio in zip(missing, await asyncio.gather(*(one(texts[i]) for i in missing))):
            audios[i] = audio
    directory = _set_dir(cache_root, language, gender)
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for number, (text, audio) in enumerate(zip(texts, audios), start=1):
        if not audio:
            continue
        name = f"{number:03d}.mp3"
        (directory / name).write_bytes(audio)
        entries.append({"text": text, "file": name})
    if entries:
        tmp = directory / "index.json.tmp"
        tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        tmp.replace(directory / "index.json")
    log_event("engine", "filler_set_built", language=language, gender=gender, phrases=len(entries), of=len(texts))


def ensure_set(
    language: str, gender: str, cache_root: Path,
    synthesize: Callable[[str, str], Awaitable[str]], resolve: Callable[[str], Awaitable[str]],
) -> None:
    """Starts building a language's set in the background if it is not there and not being built."""
    if find_set(language, gender, cache_root) is not None:
        return
    key = (language, gender)
    task = _building.get(key)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(build_set(language, gender, cache_root, synthesize, resolve))
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    _building[key] = task

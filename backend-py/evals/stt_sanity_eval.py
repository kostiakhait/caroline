r"""Golden set for the pass over speech-recognition results (app/stt_sanity.py), with its
runner. Each case: what the recognizer returned, what must come out.

  keep     the transcript must come out exactly as it went in -- the cases that matter
           most: this pass must never lose or rewrite a real utterance
  drop     nothing was said; the result must be empty
  fix      one word is a mishearing; the result must contain `want` and stay close

Run with Caroline's own Python (real model calls, well under a cent):
  set CAROLINE_APP=<...>\backend-py
  set CAROLINE_CAMERLENGO_PATH=<install>\backend-py\camerlengo
  %LOCALAPPDATA%\Caroline\runtime\python\python.exe stt_sanity_eval.py
MODE=code runs step 1 alone (no model, no network); STT_SANITY_MODEL=<slug> tries another model."""
import asyncio, os, statistics, sys, time

APP = os.environ["CAROLINE_APP"]
sys.path.insert(0, APP)
os.chdir(APP)

import app.stt_sanity as s  # noqa: E402

s.log_event = lambda *a, **k: None
MODE = os.environ.get("MODE", "full")
RUNS = int(os.environ.get("RUNS", "1"))
MODEL = os.environ.get("STT_SANITY_MODEL") or s.STT_SANITY_MODEL

CASES = [
    # --- the recognizer's own inventions on silence and noise
    ("drop", "Субтитры создавал DimaTorzok", []),
    ("drop", "Субтитры сделал DimaTorzok", []),
    ("drop", "Редактор субтитров А.Семкин Корректор А.Егорова", []),
    ("drop", "Продолжение следует...", []),
    ("drop", "Спасибо за просмотр!", []),
    ("drop", "Thanks for watching!", []),
    ("drop", "[музыка]", []),
    ("drop", "...", []),
    ("drop", "да да да да да да да да", []),
    ("drop", "ну и ну и ну и ну и ну и", []),
    ("drop", "ご視聴ありがとうございました", []),
    # what the app's own recognizer returned on 2026-10-11 for 4s of silence and of quiet noise
    ("drop", "you", []),
    ("drop", "Thank you for watching.", []),
    ("drop", "Thank you.", []),
    # --- real speech: must pass untouched
    ("keep", "да", []),
    ("keep", "Стоп.", []),
    ("keep", "Спасибо!", []),
    ("keep", "Какая погода будет завтра в Киеве?", []),
    ("keep", "Напомни мне завтра в девять позвонить маме.", []),
    ("keep", "Такс, смотри, по логике это должно работать.", []),
    ("keep", "Объясни подробно, как работает фотосинтез.", []),
    ("keep", "Ну то есть... как бы это сказать... короче, давай потом.", []),
    ("keep", "Позвони Мирославе и скажи, что я опоздаю.", []),
    ("keep", "Расскажи про квазары и пульсары.", []),
    ("keep", "Запусти билд в юнити и закоммить в гит.", []),
    ("keep", "Сколько будет семнадцать умножить на двадцать три?", []),
    ("keep", "Спасибо за просмотр моего домашнего задания, а теперь проверь ещё вот это.", []),
    ("keep", "Нет, лучше про Венеру.", ["User: Расскажи про Марс."]),
    ("keep", "В Киеве.", ["Caroline: В каком городе посмотреть погоду?"]),
    # --- one word misheard: the fix must be that word and nothing else
    ("fix", "По лохе это должно работать.", [], "по логике"),
    ("fix", "Открой мой последний до кумент.", [], "документ"),
    ("fix", "Напомни мне завтра позвонить ма ме.", [], "маме"),
    ("fix", "Какая погода будет завтра в киви?", ["Caroline: В каком городе посмотреть погоду?", "User: Я живу в Киеве."], "киеве"),
]


def check(kind, heard, result, want=None):
    out = result.text
    if kind == "drop":
        return (out == "", f"dropped ({result.by})" if out == "" else f"KEPT: {out!r}")
    if kind == "keep":
        return (out == heard.strip(), "kept" if out == heard.strip() else f"CHANGED to {out!r} ({result.verdict}, {result.by})")
    ok = want in out.lower().replace("ё", "е") and result.verdict == "fixed"
    return (ok, f"fixed: {out!r}" if ok else f"NOT fixed: {out!r} ({result.verdict})")


async def main():
    resolve = None
    if MODE != "code":
        import app.small_model_engine as eng

        async def resolve(prompt):
            return await eng.resolve_narration_prompt(prompt, model=MODEL, reasoning=s.STT_SANITY_REASONING)

    rows = []
    for case in CASES * RUNS:
        kind, heard, dialogue = case[0], case[1], case[2]
        want = case[3] if len(case) > 3 else None
        t0 = time.monotonic()
        result = await s.check_transcript(heard, dialogue, "Russian", resolve)
        ms = int((time.monotonic() - t0) * 1000)
        ok, why = check(kind, heard, result, want)
        rows.append((kind, ok, ms, result.by))
        if not ok or os.environ.get("VERBOSE"):
            print(f"{'PASS' if ok else 'FAIL'} {kind:5} {ms:>5} ms | {heard[:60]:60} | {why}", flush=True)
    for kind in ("keep", "drop", "fix"):
        mine = [r for r in rows if r[0] == kind]
        print(f"{kind:5} {sum(r[1] for r in mine)}/{len(mine)}")
    by_model = [r[2] for r in rows if r[3] == "model"]
    print(f"{MODE} ({MODEL if MODE != 'code' else 'no model'}): {sum(r[1] for r in rows)}/{len(rows)}; decided by code alone: "
          f"{sum(1 for r in rows if r[3] == 'code')} of {len(rows)}; model step median {int(statistics.median(by_model)) if by_model else 0} ms")


if __name__ == "__main__":
    asyncio.run(main())

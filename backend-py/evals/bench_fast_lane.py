"""Benchmarks fast-lane candidate models through Caroline's own try_fast_answer:
same persona, owner profile, prompt and provider key as the running app.
Run like narrator_eval.py (CAROLINE_APP, CAROLINE_CAMERLENGO_PATH, Caroline's Python).
The 2026-10-10 results are in small_model_engine.py next to FAST_LANE_MODEL."""
import asyncio, os, statistics, sys

APP = os.environ["CAROLINE_APP"]  # ...\app-xxxx\backend-py
sys.path.insert(0, APP)
os.chdir(APP)

import app.small_model_engine as e  # noqa: E402
from app.persona import get_persona  # noqa: E402
from app.workspace_dir import WORKSPACE_DIR  # noqa: E402

events = []
e.log_event = lambda comp, ev, **kw: events.append((ev, kw))

MODELS = [
    ("anthropic/claude-haiku-4.5", None),
    ("anthropic/claude-sonnet-5", None),
]
QUESTIONS = [
    ("answer", "Расскажи, что ты умеешь."),
    ("answer", "Сколько будет семнадцать умножить на двадцать три?"),
    ("answer", "Переведи на английский: я люблю читать книги."),
    ("decline", "Напомни мне завтра в девять позвонить маме."),
    ("decline", "Который сейчас час?"),
    ("decline", "Что я тебе вчера говорил про проект?"),
    ("decline", "Отправь Ивану письмо, что я опоздаю."),
    ("decline", "Какие новости сегодня?"),
]


async def main():
    persona = get_persona(WORKSPACE_DIR)
    if e.camerlengo_ai is None:
        sys.exit("camerlengo not importable here")
    rows = []
    for model, reasoning in MODELS:
        e.camerlengo_ai.Config.BENCH_MODEL = model
        e.FAST_LANE_MODEL_ATTR = "BENCH_MODEL"
        e.FAST_LANE_REASONING = reasoning
        for expect, q in QUESTIONS:
            events.clear()
            text = await e.try_fast_answer(WORKSPACE_DIR, persona, q, [], "Russian", True)
            ev, kw = events[-1] if events else ("none", {})
            got = "answer" if text else ("decline" if ev == "fast_lane_declined" else "FAIL")
            ok = got == expect
            rows.append((model, q, expect, got, kw.get("ms"), ok, (text or kw.get("error") or "")[:150].replace("\n", " ")))
            print(f"{model:30} {kw.get('ms', 0):>6} ms  {'ok ' if ok else 'BAD'} {got:8} | {q[:40]:40} | {(text or kw.get('error') or '')[:400].replace(chr(10), ' ')}", flush=True)
    print("\nSUMMARY (median ms, correct decisions):")
    for model, _ in MODELS:
        mine = [r for r in rows if r[0] == model]
        ms = [r[4] for r in mine if r[4] is not None]
        print(f"  {model:30} median {int(statistics.median(ms)) if ms else -1:>6} ms   correct {sum(r[5] for r in mine)}/{len(mine)}")


asyncio.run(main())

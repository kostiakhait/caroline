"""Benchmarks candidate models for the fast lane's GATE (small_model_engine._fast_lane_gate):
the cheapest step, which only decides whether a message can be answered without tools.
Same persona, owner profile, prompt and provider key as the running app; run like
bench_fast_lane.py (CAROLINE_APP, CAROLINE_CAMERLENGO_PATH, Caroline's Python).

A wrong "tools" for an answerable question is the costly mistake (the user gets the
whole cascade instead of a two-second answer); a wrong "answer" only costs one call of
the fast model, which declines by itself. The two are counted separately."""
import asyncio, os, statistics, sys

APP = os.environ["CAROLINE_APP"]  # ...\app-xxxx\backend-py
sys.path.insert(0, APP)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(APP)

import app.small_model_engine as e  # noqa: E402
from app.persona import get_persona  # noqa: E402
from app.workspace_dir import WORKSPACE_DIR  # noqa: E402
from bench_fast_lane import QUESTIONS  # noqa: E402
from narrator_golden import FORBIDDEN  # noqa: E402

events = []
e.log_event = lambda comp, ev, **kw: events.append((ev, kw))

MODELS = [
    ("openai/gpt-5-nano", {"effort": "minimal"}),
    ("openai/gpt-4.1-nano", None),
    ("google/gemini-2.5-flash-lite", None),
    ("openai/gpt-4.1-mini", None),
]
# Two messages from one merge window, joined the way chat_session joins them.
JOINED = [
    ("answer", "Объясни, почему небо голубое.\n\nИ ещё: сколько будет семнадцать умножить на двадцать три?"),
    ("decline", "Кто написал «Войну и мир»?\n\nИ какая погода будет завтра в Киеве?"),
    ("decline", "Какая погода будет завтра в Киеве?\n\nИ ещё скажи, кто написал «Войну и мир»."),
    ("answer", "Расскажи про Марс.\n\nНет, лучше про Венеру."),
]
RUNS = int(os.environ.get("RUNS", "1"))
if os.environ.get("ONLY"):  # e.g. ONLY=flash-lite RUNS=3 to check one candidate for stability
    MODELS = [m for m in MODELS if os.environ["ONLY"] in m[0]]


async def main():
    persona = get_persona(WORKSPACE_DIR)
    key = await e.get_model_provider_key()
    ai = e.camerlengo_ai.AI(adapter=e.camerlengo_ai.OpenRouterAdapter(api_key=key))
    summary = []
    for model, reasoning in MODELS:
        e.FAST_LANE_GATE_MODEL, e.FAST_LANE_GATE_REASONING = model, reasoning
        rows = []
        for expect, q in (list(QUESTIONS) + JOINED) * RUNS:
            events.clear()
            verdict = await e._fast_lane_gate(ai, WORKSPACE_DIR, persona, q, [], "Russian", True)
            kw = events[-1][1] if events else {}
            got = "none" if verdict is None else ("decline" if verdict[0] else "answer")
            remark = verdict[2] if verdict else None
            bad = [label for label, rx in FORBIDDEN if remark and rx.search(remark)]
            rows.append((expect, got, kw.get("ms") or 0, remark, bad))
            if got != expect or bad:
                print(f"  {model:30} {got:8} (expected {expect}) {kw.get('ms', 0):>5} ms | {q[:60].replace(chr(10), ' / ')} | {remark or kw.get('error') or ''} {bad or ''}", flush=True)
        wrong_tools = sum(1 for r in rows if r[0] == "answer" and r[1] == "decline")
        wrong_answer = sum(1 for r in rows if r[0] == "decline" and r[1] == "answer")
        none = sum(1 for r in rows if r[1] == "none")
        declines = [r for r in rows if r[1] == "decline"]
        summary.append(
            f"{model:30} median {int(statistics.median(r[2] for r in rows)):>5} ms | right {sum(r[0] == r[1] for r in rows)}/{len(rows)}"
            f" | wrong 'tools' {wrong_tools} | wrong 'answer' {wrong_answer} | no verdict {none}"
            f" | remarks {sum(1 for r in declines if r[3])}/{len(declines)}, breaking a rule {sum(1 for r in declines if r[4])}"
        )
        print(summary[-1], flush=True)
    print("\nSUMMARY")
    print("\n".join(summary))


if __name__ == "__main__":
    asyncio.run(main())

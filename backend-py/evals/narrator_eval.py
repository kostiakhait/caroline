r"""Runs the narrator golden set (narrator_golden.py) against one narrator route:
  MODE=current  generate_progress_comment (SMALL draft in English + forced translation)
  MODE=direct   generate_progress_comment_direct (NARRATOR_MODEL, drafted in Russian)
  MODE=ladder   draft_narration_remark (NARRATOR_MODEL alone since 2026-10-22) -- what the app runs

Run with Caroline's own Python, pointing at a backend-py tree and the shipped Camerlengo:
  set CAROLINE_APP=<...>\backend-py
  set CAROLINE_CAMERLENGO_PATH=<install>\backend-py\camerlengo
  set MODE=direct & set RUNS=2
  %LOCALAPPDATA%\Caroline\runtime\python\python.exe narrator_eval.py
Uses the logged-in SquirrelWisdom account (real model calls, a few cents)."""
import asyncio, os, statistics, sys, time

APP = os.environ["CAROLINE_APP"]
sys.path.insert(0, APP)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(APP)

from narrator_golden import CASES, check  # noqa: E402
import app.plugins.voice_api as v  # noqa: E402
import app.small_model_engine as eng  # noqa: E402

v.log_event = lambda *a, **k: None
drafted_by = []
eng.log_event = lambda comp, ev, **k: drafted_by.append(k.get("by")) if ev == "narration_drafted" else None
MODE = os.environ.get("MODE", "current")
RUNS = int(os.environ.get("RUNS", "2"))
# NARRATOR_MODEL=<slug> [NARRATOR_REASONING=minimal] tries another model through the direct route.
_MODEL = os.environ.get("NARRATOR_MODEL")
_REASONING = {"effort": os.environ["NARRATOR_REASONING"]} if os.environ.get("NARRATOR_REASONING") else None


async def RESOLVE(prompt):
    return await eng.resolve_narration_prompt(prompt, model=_MODEL, reasoning=_REASONING)


async def main():
    results = []
    for case in CASES:
        for _ in range(RUNS):
            activity = "\n".join(case["status"])
            t0 = time.monotonic()
            try:
                if MODE == "ladder":  # what the app does: the cheap model first, the fast one when it is silent
                    text = await eng.draft_narration_remark(case["task"], activity, list(case["said"]), "Russian", gender="female",
                                                         repeat_window=case.get("repeat_window"))
                elif MODE == "direct":
                    text = await v.generate_progress_comment_direct(
                        case["task"], activity, list(case["said"]), "Russian", RESOLVE,
                        gender="female", addressee_gender=None)
                else:
                    text = await v.generate_progress_comment(
                        "", "Russian", timeout=30.0, gender="female", activity=activity, task=case["task"],
                        addressee_gender=None, already_said=list(case["said"]),
                    )
            except Exception as exc:  # noqa: BLE001
                text = None
                print("  error:", exc)
            ms = int((time.monotonic() - t0) * 1000)
            ok, why = check(case, text)
            results.append((ok, ms))
            print(f"{'PASS' if ok else 'FAIL'} {ms:>6} ms  {case['name']:14} {why:16} | {text or '(empty)'}", flush=True)
    print(f"\n{MODE.upper()} narrator ({_MODEL or eng.NARRATOR_MODEL}): {sum(r[0] for r in results)}/{len(results)} passed, "
          f"median {int(statistics.median(r[1] for r in results))} ms")
    if drafted_by:
        print(f"the first model alone: {drafted_by.count('first')} of {len(drafted_by)} drafts; the second was asked for {drafted_by.count('second')}")


asyncio.run(main())

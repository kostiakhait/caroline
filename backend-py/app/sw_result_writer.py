"""The human-readable result of an sw turn.

Per explicit instruction (2026-10-22): a separate agent, not the verifier,
turns what the model answered into what a person reads -- the verifier only
decides whether the task is done. Seen on the test box the same day: a Word
report was made exactly as asked, and the answer the user got was the
model's own read-back of the file, "TABLE 0 / ROW 0 ['Месяц', 'Выручка'] ...".

Runs after the verifier and before reply.review / translation, for sw turns
only, on codex-mini (explicit choice; first tried on Haiku, which on the test box
turned a found-and-drafted email answer into "I can't access the mailbox without
your permission" and a verifier's complaint into a claim of success). Most answers are already fine: the model
then says OK and the answer goes out as it is. Any failure leaves the answer
as it was -- this pass must never cost the user the answer.
"""

from __future__ import annotations

import time

from app.logging_setup import log_event

RESULT_WRITER_MODEL = "openai/gpt-5.1-codex-mini"
RESULT_WRITER_REASONING = {"effort": "low"}
_UNCHANGED = "OK"

_PROMPT = (
    "CRITICAL: a program reads your reply. Reply with exactly {ok} when the answer below can go to the user "
    "as it is; otherwise reply with the rewritten answer and nothing else.\n\n"
    "An AI assistant finished a task for its user. Below is the user's request and the answer the assistant "
    "wrote. Your only job: make sure the user reads a clear, human answer.\n"
    "Rewrite it only when it is not one: raw tool output pasted in (a table dumped as rows or lists, JSON, "
    "key-value blobs, file listings, logs), a check of the work instead of its result, or text written to "
    "something other than the user. Then say in plain words what was done and what came out -- the real "
    "result, with every concrete value it contains (numbers, names, paths, dates, quoted text) copied exactly.\n"
    "Never add anything the answer does not say, never drop a result, a problem or a question it contains: "
    "a question to the user stays a question. Write in the language of the user's request. Keep it as short "
    "as the content allows.\n\n"
    "USER'S REQUEST:\n---\n{request}\n---\n\n"
    "ASSISTANT'S ANSWER:\n---\n{answer}\n---\n\n"
    "Reply now."
)


async def write_result(request: str, answer: str, tab_id: str | None = None) -> str:
    """The answer the user reads: `answer` itself, or its human rewrite."""
    from app.small_model_engine import resolve_narration_prompt

    if not answer.strip():
        return answer
    started = time.monotonic()
    try:
        raw = (await resolve_narration_prompt(_PROMPT.format(ok=_UNCHANGED, request=request, answer=answer),
                                              model=RESULT_WRITER_MODEL, reasoning=RESULT_WRITER_REASONING) or "").strip()
    except Exception as exc:  # noqa: BLE001 -- the answer as it was, never none
        log_event("engine", "sw_result_writer_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__,
                  ms=int((time.monotonic() - started) * 1000))
        return answer
    ms = int((time.monotonic() - started) * 1000)
    if not raw or raw.strip(" .\"'«»").upper() == _UNCHANGED:
        log_event("engine", "sw_result_writer", tab_id=tab_id, changed=False, ms=ms)
        return answer
    log_event("engine", "sw_result_writer", tab_id=tab_id, changed=True, ms=ms, before=answer[:400], after=raw[:400])
    return raw

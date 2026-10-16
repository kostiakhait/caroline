r"""The verifier's reply protocol (small_model_engine._build_verification_prompt,
_read_verifier_reply): "OK" / escalation / corrected text in a tag.

Two parts.
  code   what the program does with a verifier reply of each shape -- no model, no network.
  model  the real verifier model on a small golden set: a good reply must come back as
         OK (and so reach the user word for word), an unfinished one must escalate, a
         reply that claims what the tool results do not show must be corrected, and
         whatever reaches the user must never be a comment on the reply.

Run with Caroline's own Python (the model part makes real calls, a few cents):
  set CAROLINE_APP=<...>\backend-py
  set CAROLINE_CAMERLENGO_PATH=<install>\backend-py\camerlengo
  %LOCALAPPDATA%\Caroline\runtime\python\python.exe verifier_eval.py
MODE=code runs the first part alone; RUNS=<n> repeats the model part;
VERIFIER_MODEL=<slug> puts another model in the verifier's place (as VERIFIER_FIRST_MODEL would)."""
import asyncio, os, statistics, sys, time

APP = os.environ["CAROLINE_APP"]
sys.path.insert(0, APP)
os.chdir(APP)

import app.small_model_engine as e  # noqa: E402

MODE = os.environ.get("MODE", "full")
RUNS = int(os.environ.get("RUNS", "1"))
ANSWER = "Завтра в Киеве около тринадцати градусов, небольшой дождь."

CODE_CASES = [
    # (verifier reply, candidate, what must go to the user, how)
    ("OK", ANSWER, ANSWER, "ok"),
    (" ok. ", ANSWER, ANSWER, "ok"),
    ("<final>Завтра в Киеве около шестнадцати градусов, небольшой дождь.</final>", ANSWER,
     "Завтра в Киеве около шестнадцати градусов, небольшой дождь.", "corrected"),
    ("<final>Завтра в Киеве около шестнадцати градусов", ANSWER, "Завтра в Киеве около шестнадцати градусов", "corrected"),
    # The four shapes seen live, 2026-10-10/11, each of which reached the user as the answer.
    ("Ответ удовлетворяет всем условиям, дополнительная работа не требуется.", ANSWER, ANSWER, "unparsed_candidate_kept"),
    ("Поняла, приветствие соответствует запросу.", ANSWER, ANSWER, "unparsed_candidate_kept"),
    ("Принято, приветствие выполнено корректно.", ANSWER, ANSWER, "unparsed_candidate_kept"),
    ("В ответе другая помощница написала, что в Киеве тринадцать градусов. Проверка источников показывает, что это неточно.",
     ANSWER, ANSWER, "unparsed_candidate_kept"),
    # A comment put inside the tag is still a comment.
    ("<final>Ответ полностью соответствует запросу.</final>", ANSWER, ANSWER, "verdict_in_tag"),
    # An opinion in front of the answer inside the tag: only the opinion goes.
    ("<final>Похоже, прогноз верный: завтра в Киеве ожидается около тринадцати градусов и небольшой дождь.</final>", ANSWER,
     "Завтра в Киеве ожидается около тринадцати градусов и небольшой дождь.", "corrected"),
    # No candidate at all (no tier produced one): the verifier's own text is the one message there is.
    ("<final>Не удалось получить прогноз: поиск ничего не вернул.</final>", "", "Не удалось получить прогноз: поиск ничего не вернул.", "corrected"),
    ("Не удалось получить прогноз: поиск ничего не вернул.", "", "Не удалось получить прогноз: поиск ничего не вернул.", "unparsed_text_used"),
    ("OK", "", "", "ok_without_candidate"),
]

SYSTEM = "You are Caroline, a personal assistant. Answer the user in their own language, briefly."
WEATHER_TRACE = [
    'Tool call web_search (arguments: {"question": "weather forecast Kyiv tomorrow"}); result: Web search results: '
    "1. BBC Weather - Kyiv: Tomorrow light rain, high 13 C, low 9 C, wind 11 km/h -- https://www.bbc.com/weather/703448",
]
# 2026-10-11, live: what supports the reply sits past the first 300 characters of the
# result -- all the verifier was shown then, and it escalated a correct answer. The line is
# built by the engine's own _trace_line, so this case follows whatever the trace keeps.
LONG_SEARCH_RESULT = (
    "Web search results for: Kyiv weather tomorrow October 12, 2026 forecast\n"
    "1. Hourly weather forecast for Kyiv - Foreca.co.uk -- https://www.foreca.co.uk/Ukraine/Kyiv?details=20261012\n"
    "   ## Monday ... October 12 ... 10 h 58 min ... 7:14 AM 07:14 6:13 PM 18:13 ... Morning temperature of 10 °C, afternoon 14°, "
    "evening 10° and night 9°. Morning temperature of 51 degrees, afternoon 58°, evening 51° and night 49°. ... Rain, evening and "
    "night partly cloudy. ... Cloudy and light rain Gusts 11 mph 18 km/h 3 Bft 5 m/s ... Precip. chance 35% ... Overcast and light "
    "rain Gusts 13 mph 22 km/h 4 Bft 6 m/s ... Precip. chance 38%\n"
    "2. Якою буде погода в Києві та області завтра, 12 жовтня -- https://kyiv.info/article/pohoda-u-kyyevi-12-zhovtnya\n"
    "   У понеділок, 12 жовтня, у Києві хмарно, невеликий дощ. Температура вночі 8-10°, вдень 12-14°. Вітер північно-західний, "
    "5-10 м/с.\n"
    "3. BBC Weather - Kyiv -- https://www.bbc.com/weather/703448\n"
    "   Tomorrow: light rain and a gentle breeze. High 14°, low 9°. Chance of precipitation 40-60%."
)
LONG_TRACE = [e._trace_line("web_search", '{"question": "Kyiv weather tomorrow October 12, 2026 forecast"}', LONG_SEARCH_RESULT)]
MAIL_TRACE = ['Tool call mail_search (arguments: {"from": "Anna"}); result: 2 messages: "Invoice March" (2026-10-09), "Re: invoice" (2026-10-10)']

# Per explicit instruction (2026-10-15), a real incident: a sw-mode turn asked
# to check every mailbox (log in, read important messages, download
# attachments, report per mailbox) called request_info (memory) once and
# answered as if done -- no escalation, no further tool calls, task silently
# abandoned. Golden case for RETRY_SENTINEL, shaped exactly like it: a
# multi-target request, a one-call trace, no parallel stage ever run, a
# candidate that claims completion. See MEMORY_TRACE/MAILBOX_TASK below.
MEMORY_TRACE = [
    'Tool call request_info (arguments: {"query": "unread mail across accounts"}); result: '
    "Memory holds nothing relevant to this query."
]
MAILBOX_TASK = (
    "Проверь все мои почтовые ящики на новые письма: зайди в каждый, прочитай важное, скачай вложения, "
    "отчитайся по каждому ящику отдельно."
)

# (name, user text, candidate, trace, can_escalate, want): want is "ok" | "escalate" | "changed" | "not_ok" |
# "retry" (want="retry" implies can_retry=True is also passed -- see case[6] handling in main()).
MODEL_CASES = [
    ("good: weather with a source", "Какая погода будет завтра в Киеве?",
     "Завтра в Киеве небольшой дождь, днём около тринадцати градусов, ночью около девяти. Это по данным BBC Weather.",
     WEATHER_TRACE, True, "ok"),
    ("good: facts deep in a long result", "Какая погода будет завтра в Киеве?",
     "Завтра в Киеве облачно, небольшой дождь. Ночью около девяти-десяти градусов, днём двенадцать-четырнадцать. Вероятность "
     "осадков от тридцати пяти до шестидесяти процентов, ветер северо-западный, пять-десять метров в секунду.", LONG_TRACE, True, "ok"),
    ("good: plain knowledge, no tools", "Кто написал «Войну и мир»?", "«Войну и мир» написал Лев Толстой.", [], True, "ok"),
    ("good: arithmetic", "Сколько будет семнадцать умножить на двадцать три?", "Семнадцать умножить на двадцать три будет триста девяносто один.",
     [], True, "ok"),
    ("good: mail found", "Есть ли письма от Анны про счёт?",
     "Да, от Анны два письма про счёт: «Invoice March» от девятого октября и «Re: invoice» от десятого.", MAIL_TRACE, True, "ok"),
    ("good: a greeting", "Привет!", "Привет! Чем могу помочь?", [], True, "ok"),
    ("good: long explanation", "Объясни коротко, как работает фотосинтез.",
     "Фотосинтез — это процесс, при котором растения превращают свет в химическую энергию. В хлоропластах хлорофилл улавливает "
     "свет, и его энергия идёт на расщепление воды: выделяется кислород, а полученные электроны и протоны запасаются в виде АТФ "
     "и НАДФН. Затем, в цикле Кальвина, эта энергия используется, чтобы из углекислого газа собрать глюкозу. В итоге из воды, "
     "углекислого газа и света получаются сахар и кислород.", [], True, "ok"),
    ("unfinished: promise instead of work", "Отправь Анне письмо, что я опоздаю на десять минут.",
     "Хорошо, сейчас отправлю Анне письмо о том, что вы опоздаете на десять минут.", [], True, "escalate"),
    ("unfinished: claims a send no tool made", "Отправь Анне письмо, что я опоздаю на десять минут.",
     "Готово, письмо Анне отправлено.", MAIL_TRACE, True, "escalate"),
    ("wrong figure against the tool result", "Какая погода будет завтра в Киеве?",
     "Завтра в Киеве солнечно, днём около двадцати пяти градусов. Это по данным BBC Weather.", WEATHER_TRACE, True, "not_ok"),
    ("last tier, nothing was done", "Отправь Анне письмо, что я опоздаю на десять минут.", "", [], False, "changed"),
    ("multi-target abandoned after one shallow call (real incident, 2026-10-15)", MAILBOX_TASK,
     "Проверила — новых писем не найдено, все ящики в порядке.", MEMORY_TRACE, True, "retry", True),
]


def looks_like_comment(text):
    return e._looks_like_verdict(text)


async def run_model_case(ai, cfg, user_text, candidate, trace, can_escalate, can_retry=False):
    """The verifier asked the way verify_candidate asks it, the second ask included.
    Returns (raw reply that was used, ms, asked twice)."""
    tracker = e._TurnTracker()
    # Mirrors run_small_model_turn()'s own computation -- see _looks_multi_target's
    # own comment; without this, _build_verification_prompt's multi-target red flag
    # never fires here even for a case deliberately shaped to trigger it.
    tracker.looks_multi_target = e._looks_multi_target(user_text)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_text},
                {"role": "assistant", "content": candidate or "(no answer was produced)"},
                {"role": "user", "content": e._build_verification_prompt(tracker, candidate, can_escalate, trace, can_retry=can_retry)}]

    async def ask(msgs):
        return (await asyncio.to_thread(ai.resolve_agentic, msgs, None, None, model=cfg.AI_MODEL_MINI, max_iterations=1,
                                        transforms=e.DISABLE_MIDDLE_OUT, reasoning=e.VERIFIER_REASONING_EFFORT)) or ""

    t0 = time.monotonic()
    raw = await ask(messages)
    twice = False
    already_a_form = (can_escalate and e.ESCALATION_SENTINEL in raw) or (can_retry and e.RETRY_SENTINEL in raw)
    if not already_a_form and e._read_verifier_reply(raw, candidate)[1].startswith("unparsed"):
        twice = True
        again = await ask(messages + [{"role": "assistant", "content": raw},
                                      {"role": "user", "content": e._verifier_form_reminder(can_escalate, False, e._usable(candidate), can_retry)}])
        already_a_form_again = (can_escalate and e.ESCALATION_SENTINEL in again) or (can_retry and e.RETRY_SENTINEL in again)
        if already_a_form_again or not e._read_verifier_reply(again, candidate)[1].startswith("unparsed"):
            raw = again
    return raw, int((time.monotonic() - t0) * 1000), twice


async def main():
    bad = 0
    for reply, candidate, want_text, want_how in CODE_CASES:
        text, how = e._read_verifier_reply(reply, candidate)
        ok = (text, how) == (want_text, want_how)
        bad += not ok
        if not ok or os.environ.get("VERBOSE"):
            print(f"{'PASS' if ok else 'FAIL'} code | {reply[:60]!r} -> {how}: {text[:70]!r}")
    print(f"code: {len(CODE_CASES) - bad}/{len(CODE_CASES)}")
    if MODE == "code":
        return
    api_key = await e.get_model_provider_key()
    ai = e.camerlengo_ai.AI(adapter=e.camerlengo_ai.OpenRouterAdapter(api_key=api_key))
    cfg = e.camerlengo_ai.Config
    if os.environ.get("VERIFIER_MODEL"):
        class cfg:  # noqa: N801 -- stands in for Config with one name changed
            AI_MODEL_MINI = os.environ["VERIFIER_MODEL"]
        e.VERIFIER_REASONING_EFFORT = None
    rows = []
    for case in MODEL_CASES * RUNS:
        name, user_text, candidate, trace, can_escalate, want = case[:6]
        can_retry = case[6] if len(case) > 6 else False
        raw, ms, twice = await run_model_case(ai, cfg, user_text, candidate, trace, can_escalate, can_retry)
        if can_escalate and e.ESCALATION_SENTINEL in raw:
            got, sent = "escalate", ""
        elif can_retry and e.RETRY_SENTINEL in raw:
            got, sent = "retry", ""
        else:
            sent, how = e._read_verifier_reply(raw, candidate)
            got = "ok" if how == "ok" else "changed" if sent != candidate else "kept"
        if want == "not_ok":
            ok = got in ("escalate", "changed", "retry")
        else:
            ok = got == want
        if sent and looks_like_comment(sent):
            ok, got = False, got + " (a comment reached the user)"
        rows.append((want, ok, ms, len(raw), twice))
        if not ok or twice or os.environ.get("VERBOSE"):
            print(f"{'PASS' if ok else 'FAIL'} {ms:>6} ms | {name:38} want {want:8} got {got:8}{' (asked twice)' if twice else ''} | {raw[:150]!r}")
    for want in ("ok", "escalate", "retry", "not_ok", "changed"):
        mine = [r for r in rows if r[0] == want]
        if mine:
            print(f"{want:8} {sum(r[1] for r in mine)}/{len(mine)}   median {int(statistics.median(r[2] for r in mine))} ms, "
                  f"median reply {int(statistics.median(r[3] for r in mine))} chars")
    print(f"model ({cfg.AI_MODEL_MINI}): {sum(r[1] for r in rows)}/{len(rows)}; asked twice: {sum(r[4] for r in rows)}")


if __name__ == "__main__":
    asyncio.run(main())

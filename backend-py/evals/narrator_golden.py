"""Golden set for the voice narrator, in the spirit of Konstantin's microagents paper:
cases with required and forbidden outputs, deterministic checks, run against a
narrator implementation, reporting pass rate and latency."""
import re

# The engine's own fact lines, as small_model_engine.py emits them.
ACTIVITY_START = "The request has been received and work on it has started."
TIER_1 = "Tier 1 (openai/gpt-5-nano) is working."
TIER_2 = "Tier 2 (openai/gpt-5.1-codex-mini) is working."
HEARD_NOTHING = "Nothing new has happened since the last remark, and the user has heard nothing for about 10 s."

CASES = [
    dict(name="start", task="Найди погоду в Киеве на выходные.",
         status=[ACTIVITY_START], said=[], expect="say", need=["погод", "киев"]),
    dict(name="web dead end", task="Найди погоду в Киеве на выходные.",
         status=[ACTIVITY_START, TIER_1,
                 'Tool call web_search (arguments: {"question": "Kyiv weekend weather forecast"}); result: '
                 "I don't have access to live or future-specific weather data, so I can't give an exact forecast."],
         said=["Смотрю погоду в Киеве на выходные."], expect="say", need=["интернет", "поиск", "не наш", "не наход", "не дал", "не удалось", "нет данных", "нет прогноза", "нет доступ", "не могу"]),
    dict(name="memory empty", task="Найди погоду в Киеве на выходные.",
         status=[ACTIVITY_START,
                 'Tool call request_info (arguments: {"query": "Kyiv weekend weather"}); result: '
                 '{"text": "Memory holds nothing relevant to \\"Kyiv weekend weather\\"."}'],
         said=["Смотрю погоду в Киеве на выходные.", "Поиск в интернете прогноза не дал."],
         expect="say", need=["замет", "памят", "сохран", "записях", "ничего"]),
    dict(name="found mail", task="Есть ли новые письма от Анны?",
         status=[ACTIVITY_START,
                 'Tool call mail_search (arguments: {"from": "Anna", "unread": true}); result: '
                 "3 messages: 'Счёт за март', 'Re: счёт', 'Встреча в пятницу'"],
         said=["Проверяю почту от Анны."], expect="say", need=["три", "письм"]),
    dict(name="note saved", task="Сохрани заметку: встреча с Олегом в пятницу в десять.",
         status=[ACTIVITY_START,
                 'Tool call notes_create (arguments: {"title": "Встреча с Олегом"}); result: created note "Встреча с Олегом" in folder Caroline:Notes'],
         said=[], expect="say", need=["замет", "олег", "встреч"]),
    dict(name="nothing new", task="Найди погоду в Киеве на выходные.",
         status=[ACTIVITY_START, TIER_1],
         said=["Смотрю погоду в Киеве на выходные."], expect="empty_or_say", need=[]),
    dict(name="machinery bait", task="Расскажи, что ты умеешь.",
         status=[ACTIVITY_START, TIER_2,
                 "Tier 2 (openai/gpt-5.1-codex-mini) produced an answer; moving on to checking it.", "Checking the result (no tools).",
                 "Time spent on the request so far: 9 s.", "NEW since your last remark:",
                 "Tier 2 (openai/gpt-5.1-codex-mini) produced an answer; moving on to checking it.", "Checking the result (no tools)."],
         said=["Рассказываю, что я умею."], expect="empty_or_say", need=[], forbid=["закончил", "готов", "заверш"]),
    dict(name="long wait", task="Составь план поездки во Львов на три дня.",
         status=[ACTIVITY_START, TIER_1, "Time spent on the request so far: 48 s."],
         said=["Составляю план поездки во Львов."], expect="empty_or_say", need=[]),
    # 2026-10-11, live: a memory lookup started and the narrator stayed silent -- the
    # tool's name says nothing, the plugin's does.
    dict(name="memory start", task="Какая погода будет завтра в Киеве?",
         status=[ACTIVITY_START, TIER_2,
                 'Tool call request_info started (plugin "memory"; arguments: {"query": "Какая погода будет завтра в Киеве?"}).',
                 "Time spent on the request so far: 7 s.", "NEW since your last remark:",
                 'Tool call request_info started (plugin "memory"; arguments: {"query": "Какая погода будет завтра в Киеве?"}).'],
         said=["Ищу прогноз погоды на завтра в Киеве."], expect="say", need=["памят", "запис", "замет", "сохран", "помню", "знаю", "известн", "у меня"]),
    # 2026-10-11, live: 21s of silence while a search ran -- three drafts in a row came
    # back empty. Told the user has heard nothing, the narrator must say something.
    dict(name="heard nothing", task="Какая погода будет завтра в Киеве?",
         status=[ACTIVITY_START, TIER_2,
                 'Tool call web_search started (arguments: {"question": "weather forecast Kyiv tomorrow"}).',
                 "Time spent on the request so far: 24 s.", HEARD_NOTHING],
         said=["Ищу прогноз погоды на завтра в Киеве.", "Ищу прогноз в интернете."], expect="say", need=[]),
    # 2026-10-11, live: the narrator read a raw result out as the answer -- "максимум
    # шестьдесят градусов" (60 F) for Kyiv in October. The rule since main's decision of
    # the same day: a figure in a result may be said, but as something found and still
    # being confirmed, never as the settled answer (hedge_if below).
    # 2026-10-14, live: two minutes into a turn, nine remarks said, nothing new for a while.
    # Every draft was rejected as a repeat of some earlier remark and the user heard nothing
    # for 31.7s. Only the last two remarks count then (VOICE_NARRATION_STALE_REPEAT_WINDOW).
    dict(name="long turn, silent again", task="Какой сегодня курс доллара к гривне?",
         status=[ACTIVITY_START, TIER_1, "Tier 1 (openai/gpt-5-nano) produced an answer; moving on to checking it.",
                 "Checking the result (no tools).", "The check did not confirm the task was done; escalating to tier 2.",
                 "Tier 2 (openai/gpt-5.1-codex-mini) is working.", "Time spent on the request so far: 104 s.",
                 "Nothing new has happened since the last remark, and the user has heard nothing for about 10 s."],
         said=["Ищу курс доллара к гривне.", "Ищу курс в интернете.", "Нашла курс и сейчас его проверяю.",
               "Всё ещё проверяю курс доллара к гривне.", "Продолжаю искать курс на сайте банка.",
               "Работа над курсом ещё идёт.", "Пока всё ещё уточняю курс.", "Ищу данные о курсе на другом сайте.",
               "Всё ещё работаю над вашим вопросом про курс."],
         repeat_window=2, expect="say", need=[]),
    dict(name="answer unchecked", task="Сколько дней осталось до Нового года?",
         status=[ACTIVITY_START, TIER_1,
                 "Tool call get_current_time (arguments: {}); result: 2026-10-11 15:27",
                 "Tier 1 (openai/gpt-5-nano) produced an answer; moving on to checking it.", "Checking the result (no tools).",
                 "Time spent on the request so far: 14 s.", "NEW since your last remark:",
                 "Checking the result (no tools)."],
         said=["Проверяю текущую дату.", "Узнала текущую дату, теперь считаю дни до Нового года."],
         expect="empty_or_say", need=[], forbid=["закончил", "готов", "заверш", "посчитал"]),
    dict(name="raw figures", task="Какая погода будет завтра в Киеве?",
         status=[ACTIVITY_START, TIER_2,
                 'Tool call web_search (arguments: {"question": "weather forecast Kyiv tomorrow"}); result: '
                 "Tomorrow in Kyiv: cloudy with rain in the morning, high 60 F, low 52 F, wind 9 mph.",
                 "Time spent on the request so far: 41 s.", "NEW since your last remark:",
                 'Tool call web_search (arguments: {"question": "weather forecast Kyiv tomorrow"}); result: '
                 "Tomorrow in Kyiv: cloudy with rain in the morning, high 60 F, low 52 F, wind 9 mph."],
         said=["Ищу прогноз погоды на завтра в Киеве.", "Ищу прогноз в интернете."], expect="say",
         need=["наш", "прогноз", "есть"], hedge_if=["шестьдесят", "пятьдесят", "градус", "девят"]),
    # A found figure is not a failure of the data: with figures masked (this branch's
    # first approach) "Latest quote: N (-N%)" was narrated as "данные не загрузились полностью".
    dict(name="figure in a result", task="Какой сегодня курс доллара к гривне?",
         status=[ACTIVITY_START, TIER_1,
                 'Tool call web_search (arguments: {"question": "current USD to UAH exchange rate today"}); result: Web search '
                 "results for: current USD to UAH exchange rate today 1. USDUAH | Latest quote: 44.852664 (-0.11%) | As of "
                 "2026-10-11T20:25:10Z -- https://exa.ai/library/markets/forex/USDUAH # USDUAH The latest quote",
                 "Time spent on the request so far: 9 s.", "NEW since your last remark:",
                 'Tool call web_search (arguments: {"question": "current USD to UAH exchange rate today"}); result: Web search '
                 "results for: current USD to UAH exchange rate today 1. USDUAH | Latest quote: 44.852664 (-0.11%) | As of "
                 "2026-10-11T20:25:10Z -- https://exa.ai/library/markets/forex/USDUAH # USDUAH The latest quote"],
         said=["Ищу курс доллара к гривне."], expect="say", need=["наш", "курс", "есть"],
         forbid=["не загруз", "неполн", "не полност", "частичн", "ошибк", "не удалось"], hedge_if=["сорок"]),
]

# A figure from a result is said as found and still being confirmed: one of these with it
# (the words of voice_api._FOUND_OR_CHECKING; "около" alone does not make it preliminary).
HEDGES = ("нашл", "найден", "получил", "узнал", "выяснил", "вижу", "уточня", "проверя", "перепровер", "сверя", "подтвержд",
          "убежда", "предварительн")

FORBIDDEN = [
    ("machinery", re.compile(r"web_search|request_info|notes_|mail_search|инструмент|уровень|уровн[яе]|\btier\b|gpt|codex|nano|модел|верификац", re.I)),
    ("promise", re.compile(r"\b(я )?(сделаю|проверю|найду|отправлю|сообщу|напишу|вернусь)\b|\bскоро\b|\bсейчас (сделаю|проверю|найду)", re.I)),
    ("question", re.compile(r"\?")),
    ("digits", re.compile(r"\d")),  # read aloud: numbers belong in words
    ("third person", re.compile(r"каролайн|caroline|пользовател", re.I)),
    # The persona is female: no masculine first-person past forms ("я искал", "Нашёл ...").
    ("wrong gender", re.compile(r"\bя (\w+ )?(искал|нашёл|нашел|сделал|проверил|понял|начал|сохранил|создал|посмотрел|получил|смог)\b"
                              r"|^(искал|нашёл|нашел|сделал|проверил|понял|начал|сохранил|создал|посмотрел|получил)\b", re.I)),
]


def norm(s):
    return re.findall(r"\w+", s.lower().replace("ё", "е"))


_OUTCOME = re.compile(r"\b(?:(нашл|найден|получил|узнал|выяснил|сохранил|отправил|создал|записал)\w*|(ничего|нет|не))\b")


def _outcomes(s):
    return {m.group(1) or m.group(2) for m in _OUTCOME.finditer(s.lower().replace("ё", "е"))}


def is_repeat(a, b):
    if _outcomes(a) != _outcomes(b):  # as voice_api._is_repeat: a different outcome word is news by itself
        return False
    wa, wb = set(norm(a)), set(norm(b))
    wa, wb = {w for w in wa if len(w) > 2}, {w for w in wb if len(w) > 2}  # as voice_api._is_repeat
    return len(wa) >= 3 and len(wb) >= 3 and len(wa & wb) / len(wa | wb) >= 0.6


def check(case, text):
    """Returns (passed, reason)."""
    text = (text or "").strip()
    if not text:
        return (case["expect"] != "say", "empty" if case["expect"] == "say" else "ok (empty)")
    letters = [c for c in text if c.isalpha()]
    if letters and sum("Ѐ" <= c <= "ӿ" for c in letters) / len(letters) < 0.8:
        return False, "not Russian"
    for label, rx in FORBIDDEN:
        if rx.search(text):
            return False, label
    for word in case.get("forbid", ()):
        if word in text.lower().replace("ё", "е"):
            return False, f"says {word!r}"
    low = text.lower().replace("ё", "е")
    if any(w in low for w in case.get("hedge_if", ())) and not any(h in low for h in HEDGES):
        return False, "a figure stated as settled"
    # As voice_api.generate_progress_comment_direct with repeat_window (the stale case).
    for prev in case["said"][-case["repeat_window"]:] if case.get("repeat_window") else case["said"]:
        if is_repeat(text, prev):
            return False, "repeat"
    if case["need"] and not any(n in text.lower().replace("ё", "е") for n in case["need"]):
        return False, "missed the fact"
    return True, "ok"

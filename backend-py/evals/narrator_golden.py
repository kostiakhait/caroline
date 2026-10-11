"""Golden set for the voice narrator, in the spirit of Konstantin's microagents paper:
cases with required and forbidden outputs, deterministic checks, run against a
narrator implementation, reporting pass rate and latency."""
import re

ACTIVITY_START = "Запрос получен, работа над ним началась."

CASES = [
    dict(name="start", task="Найди погоду в Киеве на выходные.",
         status=[ACTIVITY_START], said=[], expect="say", need=["погод", "киев"]),
    dict(name="web dead end", task="Найди погоду в Киеве на выходные.",
         status=[ACTIVITY_START, "Работает уровень 1 (openai/gpt-5-nano).",
                 'Вызов инструмента web_search (аргументы: {"question": "Kyiv weekend weather forecast"}); результат: '
                 "I don't have access to live or future-specific weather data, so I can't give an exact forecast."],
         said=["Смотрю погоду в Киеве на выходные."], expect="say", need=["интернет", "поиск", "не наш", "не наход", "не дал", "не удалось", "нет данных", "нет прогноза", "нет доступ", "не могу"]),
    dict(name="memory empty", task="Найди погоду в Киеве на выходные.",
         status=[ACTIVITY_START,
                 'Вызов инструмента request_info (аргументы: {"query": "Kyiv weekend weather"}); результат: '
                 '{"text": "Memory holds nothing relevant to \\"Kyiv weekend weather\\"."}'],
         said=["Смотрю погоду в Киеве на выходные.", "Поиск в интернете прогноза не дал."],
         expect="say", need=["замет", "памят", "сохран", "записях", "ничего"]),
    dict(name="found mail", task="Есть ли новые письма от Анны?",
         status=[ACTIVITY_START,
                 'Вызов инструмента mail_search (аргументы: {"from": "Anna", "unread": true}); результат: '
                 "3 messages: 'Счёт за март', 'Re: счёт', 'Встреча в пятницу'"],
         said=["Проверяю почту от Анны."], expect="say", need=["три", "3", "письм"]),
    dict(name="note saved", task="Сохрани заметку: встреча с Олегом в пятницу в десять.",
         status=[ACTIVITY_START,
                 'Вызов инструмента notes_create (аргументы: {"title": "Встреча с Олегом"}); результат: created note "Встреча с Олегом" in folder Caroline:Notes'],
         said=[], expect="say", need=["замет", "олег", "встреч"]),
    dict(name="nothing new", task="Найди погоду в Киеве на выходные.",
         status=[ACTIVITY_START, "Работает уровень 1 (openai/gpt-5-nano)."],
         said=["Смотрю погоду в Киеве на выходные."], expect="empty_or_say", need=[]),
    dict(name="machinery bait", task="Расскажи, что ты умеешь.",
         status=[ACTIVITY_START, "Работает уровень 2 (openai/gpt-5.1-codex-mini).",
                 "Уровень 2 (openai/gpt-5.1-codex-mini) дал ответ, переходим к проверке.", "Проверка результата (без инструментов)."],
         said=["Рассказываю, что я умею."], expect="empty_or_say", need=[]),
    dict(name="long wait", task="Составь план поездки во Львов на три дня.",
         status=[ACTIVITY_START, "Работает уровень 1 (openai/gpt-5-nano).", "Время работы над запросом: 48 с."],
         said=["Составляю план поездки во Львов."], expect="empty_or_say", need=[]),
]

FORBIDDEN = [
    ("machinery", re.compile(r"web_search|request_info|notes_|mail_search|инструмент|уровень|уровн[яе]|\btier\b|gpt|codex|nano|модел|верификац", re.I)),
    ("promise", re.compile(r"\b(я )?(сделаю|проверю|найду|отправлю|сообщу|напишу|вернусь)\b|\bскоро\b|\bсейчас (сделаю|проверю|найду)", re.I)),
    ("question", re.compile(r"\?")),
    ("third person", re.compile(r"каролайн|caroline|пользовател", re.I)),
]


def norm(s):
    return re.findall(r"\w+", s.lower().replace("ё", "е"))


def is_repeat(a, b):
    wa, wb = set(norm(a)), set(norm(b))
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
    for prev in case["said"]:
        if is_repeat(text, prev):
            return False, "repeat"
    if case["need"] and not any(n in text.lower().replace("ё", "е") for n in case["need"]):
        return False, "missed the fact"
    return True, "ok"

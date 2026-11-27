"""The verifier's objection written inside the tag as if it were the reply (2026-11-26, from the October live log):
while a stronger tier or a retry is possible it is the note of what is missing, not a text to send; at the last tier
the verifier is asked once more for the message the person reads. Real replies -- corrections, honest failures in the
person's terms -- are untouched. Run: python -m pytest test_verifier_objection.py -q"""

import pytest

from app import small_model_engine as e

# Sent as the reply, October 2026 (the log's small_model_verification_done), each unfit by a second model's labels.
OBJECTIONS = [
    "The reply is incorrect: the tool output shows only that a reminder was scheduled and that a backup completed at "
    "16:00, but there is no evidence of a 17:00 memory backup or vault update. Please perform the missing backup action.",
    "The requested backup note creation has not been documented: you still need to review the current Caroline:Vault "
    "content and confirm the backup details, then report the actual status.",
    "The assistant made no tool calls and thus did not retrieve or update the backup note.",
    "No tool calls were made, so the vault backup note was not updated and memory was not saved.",
    "No save_info call was made, so the periodic memory backup execution was not recorded.",
    "Plan recorded but no steps were performed because no tools were available; nothing was changed or verified.",
    "The task was not completed: the two plan steps (“Gather current reminder details” and “Update the note”) were "
    "declared but never executed.",
    "Ответ не завершён: объявлен план из трёх шагов, но ни один шаг не был выполнен, данные прогноза не получены.",
    "План с тремя параллельными шагами был объявлен, но ни один шаг не отмечен как выполненный, поэтому цены не обновлены.",
    "Не удалось завершить поиск: был объявлен план из трёх шагов, но ни один шаг не был отмечен как выполненный.",
]

# Also sent as the reply then, and fit to send: corrections and an honest failure in the person's terms.
REPLIES = [
    "На сегодня, двенадцатого октября, официальный курс Национального банка Украины составляет сорок четыре гривны.",
    "Вот уточнённый прогноз на пятнадцатое октября для Киева. По данным Яндекс-Погоды: утром около плюс семи градусов.",
    "Извиняюсь, но я проверила доступные источники (Exa.ai, BBC Weather и другие) и не нашла в них чёткого прогноза "
    "погоды на завтрашний день.",
    "В трёх магазинах iStore.ua, McStore.ua и eplio.com.ua цены на iPhone 17 256 ГБ Black таковы: iStore.ua — 41 099 грн.",
    "Коротко: открытие сезона Буковеля в этом году — девятнадцатое декабря.",
    "Задача выполнена: письмо отправлено Анне.",
    # Words close to the markers, said to the person about their own things.
    "Your answer to question 3 is incorrect: 7 × 8 is 56, not 54.",
    "Ответ неверный: семью восемь — пятьдесят шесть, а не пятьдесят четыре.",
    "I checked your essay: the answer to the second question is wrong, the war ended in 1945.",
    "Чтобы получить визу, вам нужно подать документы до пятницы; шаги такие: анкета, фото, оплата.",
]


@pytest.mark.parametrize("text", OBJECTIONS)
def test_an_objection_is_not_sent_while_someone_can_still_finish_the_task(text):
    assert e._objection_action(text, can_escalate=True, can_retry=True) == "up"
    assert e._objection_action(text, can_escalate=False, can_retry=True) == "retry"
    assert e._objection_action(text, can_escalate=False, can_retry=False) == "ask"


@pytest.mark.parametrize("text", REPLIES)
def test_a_real_reply_is_left_alone(text):
    assert e._objection_action(text, can_escalate=True, can_retry=True) is None


def test_the_last_reminder_asks_for_the_persons_message_in_the_tag():
    assert "<final></final>" in e._VERIFIER_OBJECTION_REMINDER
    assert "never saw" in e._VERIFIER_OBJECTION_REMINDER


def test_the_prompt_says_a_mark_is_bookkeeping_and_never_to_mention_the_plan():
    tracker = e._TurnTracker()
    tracker.plan_steps = ["find prices at store A", "find prices at store B"]
    prompt = e._build_verification_prompt(tracker, "", False, ["Tool call web_search; result: A — 41 099 грн"])
    assert "A mark is bookkeeping" in prompt
    assert "never mention plans, steps or marks" in prompt
    assert "The person never saw the plan" in prompt

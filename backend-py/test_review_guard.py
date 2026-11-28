from app.plugins.voice_api import is_commentary


def test_a_comment_about_the_text_is_not_a_rewrite():
    original = "Перезапуск просмотрщика завершён и готов к следующему шагу."
    comment = ("Текст не относится к текущей задаче: он сообщает о перезапуске инструмента, а задача — создать игру. "
               "Рекомендую заменить на сообщение, которое информирует пользователя о следующем шаге разработки игры.")
    assert is_commentary(original, comment)


def test_a_rewrite_of_about_the_same_length_is_kept():
    assert not is_commentary("Смотрю, как называются материалы кузова в модели Corvette.", "Смотрю, как называются материалы кузова.")
    assert not is_commentary("Готово.", "Готово, вот ссылка.")

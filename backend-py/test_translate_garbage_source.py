"""translate_text's garbage check against the source (2026-11-23): a tell the original reply has itself -- an apology,
a bracket at the start, a name with Ukrainian letters -- does not throw a correct translation away; a translator's own
refusal or JSON still does. Run: python -m pytest test_translate_garbage_source.py -q"""

from app.plugins.voice_api import _looks_like_narration_garbage as garbage

RU = "Russian"


def test_an_apologising_reply_keeps_its_translation():
    src = "I'm sorry, I can't open the file: it is damaged. Send it again and I will try once more."
    tr = "Извините, не могу открыть файл: он повреждён. Пришлите его ещё раз, и я попробую снова."
    assert garbage(tr, RU, max_chars=1000)                       # without the source: thrown away, as before
    assert not garbage(tr, RU, max_chars=1000, source=src)


def test_the_translators_own_refusal_is_still_garbage():
    assert garbage("Извините, не могу помочь с этим.", RU, max_chars=1000, source="The report is attached, three pages.")
    # a source that mentions "can't" somewhere does not excuse a short refusal in place of its translation
    src = "The store can't deliver today, so I booked the pickup for Friday at 10 and paid the deposit of 20 euros."
    assert garbage("Извините, не могу помочь с этим запросом.", RU, max_chars=1000, source=src)


def test_brackets_and_ukrainian_names_from_the_source_are_kept():
    assert not garbage("[1] Первый пункт: билеты.", RU, max_chars=1000, source="[1] First item: the tickets.")
    assert garbage('{"text": "Готово"}', RU, max_chars=1000, source="Done.")
    src = "The «Сторінка товару» page of Foxtrot shows the price."
    assert not garbage("Страница «Сторінка товару» на Foxtrot показывает цену.", RU, max_chars=1000, source=src)
    assert garbage("Сторінка товару показує ціну.", RU, max_chars=1000, source="The product page shows the price.")


def test_the_narration_draft_check_is_unchanged():
    assert garbage("I'm sorry, I can't help with that.", draft_only=True)

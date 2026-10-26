"""The language of a turn comes from the user's own message (app/language_hint.py).
Run: python -m pytest test_language_hint.py -q"""

from app.language_hint import guess_language_from_text as guess


def test_a_russian_message_is_russian():
    assert guess("Ты знаешь кто я?") == "Russian"


def test_a_ukrainian_message_is_ukrainian():
    assert guess("Привіт, як справи?") == "Ukrainian"


def test_an_english_message_says_nothing():
    assert guess("Do you know who I am?") is None


def test_too_little_to_say_is_none():
    assert guess("") is None
    assert guess("?") is None
    assert guess("я") is None


def test_mostly_latin_with_a_russian_word_is_not_russian():
    assert guess("Please open the file Привет.txt now") is None

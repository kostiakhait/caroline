"""Word-for-word checks that fold typography the same way (2026-11-23): a quote with ’ is the page's ', a pointer
copied with "..." is the message's «...», and a quote of markup alone supports nothing.
Run: python -m pytest test_text_match_typography.py -q"""

from app import web_research
from app.microagents import delivery_completeness, sw_result_facts


def test_a_web_quote_with_other_quote_marks_is_still_in_the_page():
    page = "It's 5 million people — the 2021 census — and growing."
    assert web_research.quote_in_text("It’s 5 million people", page)                 # was dropped
    assert web_research.quote_in_text("the 2021 census - and growing", page)
    assert not web_research.quote_in_text("It’s 6 million people", page)
    assert not web_research.quote_in_text("“ ”", page)                                # nothing but marks


def test_a_markup_only_quote_supports_nothing():
    given = {"result": "Plots **North** and South: 40 sensors.", "statements": ["40 sensors"]}
    output = {"found": [{"statement": 0, "quote": "**"}, {"statement": 0, "quote": "40 sensors"}]}
    kept, dropped = sw_result_facts.kept_quotes(given, output)
    assert kept == ["40 sensors"] and dropped == ["**"]


def test_a_pointer_copied_with_straight_quotes_matches_the_message():
    message = 'Ответ Peacock я не писала. Если нужен, покажу «черновик от вашего имени».'
    good = {"complete": False, "missing": "no draft follows", "pointer": 'покажу "черновик от вашего имени"'}
    assert delivery_completeness.verify_completeness({"final_text": message}, good) is None     # was rejected
    bad = {"complete": False, "missing": "x", "pointer": "Вот черновик ответа"}
    assert delivery_completeness.verify_completeness({"final_text": message}, bad) is not None

"""The checks of the web search microagents (app/microagents/web_research_agents.py; no test before 2026-11-22)
and the sources an answer goes out with (web_research.cited_facts). Run: python -m pytest test_web_research_agents.py -q"""

from app import web_research as wr
from app.microagents import web_research_agents as wa
from app.web_browser import ENGINES

FACTS = [{"label": str(i), "statement": f"fact {i}", "url": f"https://x.org/{i}"} for i in range(1, 4)]


def test_plan_needs_text_and_known_engines():
    engine = ENGINES[0]
    assert wa.verify_plan({}, {"queries": [{"query": "weather Kyiv", "engines": [engine]}]}) is None
    assert "no text" in wa.verify_plan({}, {"queries": [{"query": " ", "engines": [engine]}]})
    assert "no search engine" in wa.verify_plan({}, {"queries": [{"query": "q", "engines": []}]})
    assert "unknown engine" in wa.verify_plan({}, {"queries": [{"query": "q", "engines": ["altavista"]}]})


def test_pick_only_offered_and_once():
    offered = {"results": [{"label": "1"}, {"label": "2"}]}
    assert wa.verify_pick(offered, {"open": ["2", "1"]}) is None
    assert "not offered" in wa.verify_pick(offered, {"open": ["3"]})
    assert "twice" in wa.verify_pick(offered, {"open": ["1", "1"]})


def test_extract_needs_quotes_and_offered_links():
    offered = {"links": [{"label": "1"}]}
    assert wa.verify_extract(offered, {"facts": [{"statement": "s", "quote": "q"}], "follow": ["1"]}) is None
    assert "quote" in wa.verify_extract(offered, {"facts": [{"statement": "s", "quote": ""}], "follow": []})
    assert "not offered" in wa.verify_extract(offered, {"facts": [], "follow": ["2"]})


def test_judge_says_what_is_missing():
    assert wa.verify_judge({}, {"enough": True, "missing": ""}) is None
    assert "missing" in wa.verify_judge({}, {"enough": False, "missing": ""})
    assert "true or false" in wa.verify_judge({}, {"enough": "yes"})


def test_cited_labels_reads_every_mark_form():
    assert wa.cited_labels("A [2]. B [3][1]. C [2, 3]. D [12]") == ["2", "3", "1", "12"]
    assert wa.cited_labels("no marks [a] [] here") == []


def test_answer_may_not_cite_a_fact_that_does_not_exist():
    facts = {"facts": FACTS}
    assert wa.verify_answer(facts, {"answer": "It rains [1][3].", "used": ["1", "3"]}) is None
    assert "do not exist" in wa.verify_answer(facts, {"answer": "It rains [1].", "used": ["9"]})
    assert "cites fact number(s) ['7']" in wa.verify_answer(facts, {"answer": "It rains [7].", "used": ["1"]})


def test_every_mark_in_the_answer_gets_its_source_line():
    facts = [wr.Fact(label=str(i), statement=f"fact {i}", quote="q", url=f"https://x.org/{i}", title="t") for i in range(1, 5)]
    cited = wr.cited_facts("Rain [3], then sun [1].", ["1"], facts)          # "3" was left out of "used"
    assert [f.label for f in cited] == ["3", "1"]
    cited = wr.cited_facts("Rain [2].", ["4", "2"], facts)                  # text order first, then the rest of "used"
    assert [f.label for f in cited] == ["2", "4"]
    assert wr.cited_facts("Rain [9].", [], facts) == []                      # nothing behind a mark: no line

"""sw.result.facts (app/microagents/sw_result_facts.py) and the content-grounding check that uses it
(small_model_engine._content_grounded): a long result is read whole, in parts; a quote has to be in the result
word for word; the grounding judge sees the quotes, not the turn's results cut to 200 characters.
Run: python -m pytest test_sw_result_facts.py -q"""

import threading

import pytest

from app.microagents import sw_result_facts as rf

pytest.importorskip("claude_agent_sdk")
from app import microagent_client, small_model_engine as sme  # noqa: E402


def test_parts_cover_the_whole_text_with_overlap():
    text = "".join(f"{i:05d}" for i in range(6000))  # 30000 chars
    parts = rf.parts(text)
    assert len(parts) > 1 and all(len(p) <= rf.PART_CHARS for p in parts)
    assert parts[0][-rf.PART_OVERLAP:] == parts[1][:rf.PART_OVERLAP]
    assert text.endswith(parts[-1][-100:])
    assert rf.parts("short") == ["short"]


def test_only_word_for_word_quotes_are_kept():
    given = {"statements": ["40 sensors were used"], "result": "We deployed 40 capacitive\n sensors at 10 cm."}
    output = {"found": [{"statement": 1, "quote": "We deployed 40 capacitive sensors"},
                        {"statement": 1, "quote": "We used 40 sensors"}]}
    assert rf.verify_result_facts(given, output) is None  # a paraphrase is dropped, not refused
    assert rf.kept_quotes(given, output) == (["We deployed 40 capacitive sensors"], ["We used 40 sensors"])
    assert rf.verify_result_facts(given, {"found": []}) is None
    assert "statement" in rf.verify_result_facts(given, {"found": [{"statement": 2, "quote": "We deployed"}]})


class FakeRuntime:
    """Answers each microagent as a model would, and records what the grounding judge was shown."""

    def __init__(self, seen: dict):
        self.seen = seen

    def register(self, agent):
        pass

    def new_context(self, budget=None):
        return None

    def run(self, name, agent_input, context):
        with self.seen["lock"]:
            self.seen.setdefault(name, []).append(agent_input)
        if name == "sw.content.extract":
            return {"status": "ok", "output": {"facts": ["the sensors were deployed in Kisumu county"], "requests": []}}
        if name == rf.RESULT_FACTS:
            hit = "Kisumu county" in agent_input["result"]
            quote = "on 20 maize plots in Kisumu county"
            return {"status": "ok", "output": {"found": [{"statement": 1, "quote": quote}] if hit else []}}
        if name == "sw.content.grounding":
            supported = any("Kisumu" in line for line in agent_input["calls"])
            return {"status": "ok", "output": {"invented": [] if supported else ["Kisumu county: not in the results"]}}
        return {"status": "error", "error": "unexpected agent " + name}


@pytest.fixture
def runtime(monkeypatch):
    seen: dict = {"lock": threading.Lock()}
    monkeypatch.setattr(microagent_client, "build_local_runtime", lambda *a, **k: FakeRuntime(seen))
    monkeypatch.setattr(microagent_client, "new_budget", lambda: None)
    return seen


def test_grounding_finds_a_fact_far_into_a_long_result(runtime):
    document = "x" * 20000 + " We deployed 40 sensors on 20 maize plots in Kisumu county. " + "y" * 5000
    grounded, invented = sme._content_grounded(
        "write the Introduction", "akinakes_publish_result", {"content": "Sensors were deployed in Kisumu county."},
        [('document_get({"doc_id": "d1"})', document)], None,
    )
    assert grounded and invented == ""
    assert len(runtime[rf.RESULT_FACTS]) == len(rf.parts(document)) > 1
    assert any("Kisumu" in line for line in runtime["sw.content.grounding"][0]["calls"])


def test_a_part_that_cannot_be_read_does_not_switch_the_check_off(runtime, monkeypatch):
    real_run = FakeRuntime.run

    def flaky(self, name, agent_input, context):
        if name == rf.RESULT_FACTS and "broken" in agent_input["result"]:
            return {"status": "error", "error": {"code": "ladder_exhausted"}}
        return real_run(self, name, agent_input, context)

    monkeypatch.setattr(FakeRuntime, "run", flaky)
    grounded, invented = sme._content_grounded(
        "write the Introduction", "akinakes_publish_result", {"content": "Sensors were deployed in Kisumu county."},
        [("save_info({})", "broken"), ('document_get({"doc_id": "d1"})', "No place is named here.")], None,
    )
    assert not grounded and "Kisumu" in invented


def test_a_fact_no_result_states_is_invented(runtime):
    grounded, invented = sme._content_grounded(
        "write the Introduction", "akinakes_publish_result", {"content": "Sensors were deployed in Kisumu county."},
        [('document_get({"doc_id": "d1"})', "A document about soil moisture, no place named.")], None,
    )
    assert not grounded and "Kisumu" in invented


def test_a_faithful_copy_matches_despite_escapes_and_typography():
    # The result as it reaches the check: JSON text, its line break an escape, plain quotes and a hyphen.
    given = {"statements": ["x"], "result": '{"text": "The plots \\"North-1\\" and\nSouth used 40 sensors..."}'}
    output = {"found": [{"statement": 1, "quote": "The plots «North–1» and South used **40** sensors…"}]}
    assert rf.kept_quotes(given, output) == (["The plots «North–1» and South used **40** sensors…"], [])


def test_readable_unwraps_json_and_keeps_the_rest():
    raw = '{"status": "done", "result": {"text": "{\\"title\\": \\"Methods\\", \\"body\\": \\"Line one.\\nLine two.\\"}"}}\n[Memory: save it]'
    text = rf.readable(raw)
    assert "Line one.\nLine two." in text and "title: Methods" in text and text.endswith("[Memory: save it]")
    assert rf.readable("plain text, not JSON") == "plain text, not JSON"
    assert rf.readable("{broken json") == "{broken json"


def test_a_missed_quote_gets_one_more_try_with_the_miss_named(runtime, monkeypatch):
    real_run = FakeRuntime.run
    tries = []

    def paraphrase_first(self, name, agent_input, context):
        if name == rf.RESULT_FACTS and "Kisumu" in agent_input["result"]:
            tries.append(list(agent_input.get("rejected") or []))
            quote = "sensors went to Kisumu" if len(tries) == 1 else "on 20 maize plots in Kisumu county"
            return {"status": "ok", "output": {"found": [{"statement": 1, "quote": quote}]}}
        return real_run(self, name, agent_input, context)

    monkeypatch.setattr(FakeRuntime, "run", paraphrase_first)
    grounded, invented = sme._content_grounded(
        "write the Introduction", "akinakes_publish_result", {"content": "Sensors were deployed in Kisumu county."},
        [('document_get({"doc_id": "d1"})', "We deployed 40 sensors on 20 maize plots in Kisumu county.")], None,
    )
    assert tries == [[], ["sensors went to Kisumu"]]
    assert "the RESULT word for word" in rf.prompt_result_facts({"statements": ["x"], "result": "r", "rejected": ["q"]}, None)
    assert grounded and invented == ""


def _asking_runtime(monkeypatch, answers: list[str], matches: bool = True) -> list:
    """The text asks its reader to do something; sw.request.asked answers with each quote of `answers` in turn, and
    sw.request.matches says whether the quoted words ask for that request."""
    tries = []

    def run(self, name, agent_input, context):
        if name == "sw.content.extract":
            return {"status": "ok", "output": {"facts": [], "requests": ["asks the reader to set the objective to: soil moisture"]}}
        if name == "sw.request.asked":
            tries.append(agent_input.get("rejected"))
            return {"status": "ok", "output": {"quote": answers[min(len(tries), len(answers)) - 1]}}
        if name == "sw.request.matches":
            return {"status": "ok", "output": {"asks": matches}}
        return {"status": "error", "error": "unexpected agent " + name}

    monkeypatch.setattr(FakeRuntime, "run", run)
    return tries


def test_a_request_quote_not_in_the_task_gets_one_more_try_with_the_miss_named(runtime, monkeypatch):
    """Live 2026-11-01: nano quoted the task's words with the colon dropped, and the hand-off was refused."""
    from app.microagents import sw_content_grounding as g
    tries = _asking_runtime(monkeypatch, ["set the objective to soil moisture", "Set the objective to: soil moisture"])
    grounded, invented = sme._content_grounded(
        "- Goal: Set the objective to: soil moisture", "hand_to_agents", {"text": "Set the objective to: soil moisture"}, [], None,
    )
    assert tries == [None, "set the objective to soil moisture"]
    assert "NOT in the TASK word for word" in g.prompt_request_asked({"task": "t", "request": "r", "rejected": "q"}, None)
    assert grounded and invented == ""


def test_an_empty_request_quote_is_not_retried_and_the_request_is_refused(runtime, monkeypatch):
    tries = _asking_runtime(monkeypatch, [""])
    grounded, invented = sme._content_grounded(
        "- Goal: write the Introduction", "hand_to_agents", {"text": "Please set the objective."}, [], None,
    )
    assert tries == [None]
    assert not grounded and "the task does not ask for this" in invented


def test_words_that_are_in_the_task_but_ask_for_something_else_do_not_count(runtime, monkeypatch):
    """Live 2026-11-01: after the retry nano quoted any sentence of the task ("Do you need a lift?" for "promises to
    pick her up"); the quote is in the task, so only sw.request.matches can refuse it."""
    from app.microagents import sw_content_grounding as g
    _asking_runtime(monkeypatch, ["Set the objective to: soil moisture"], matches=False)
    grounded, invented = sme._content_grounded(
        "- Goal: Set the objective to: soil moisture", "hand_to_agents", {"text": "Set the objective to: soil moisture"}, [], None,
    )
    assert not grounded and "the task does not ask for this" in invented
    prompt = g.prompt_request_matches({"request": "promises to pick her up", "quote": "Do you need a lift?"}, None)
    assert prompt.index("Do you need a lift?") < prompt.index("promises to pick her up")  # the request last
    assert g.verify_request_matches({}, {"asks": "yes"}) and g.verify_request_matches({}, {"asks": True}) is None

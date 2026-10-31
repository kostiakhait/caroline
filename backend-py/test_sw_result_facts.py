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


def test_a_quote_must_be_in_the_result_word_for_word():
    given = {"statements": ["40 sensors were used"], "result": "We deployed 40 capacitive\n sensors at 10 cm."}
    assert rf.verify_result_facts(given, {"found": [{"statement": 1, "quote": "We deployed 40 capacitive sensors"}]}) is None
    assert rf.verify_result_facts(given, {"found": []}) is None
    assert "word for word" in rf.verify_result_facts(given, {"found": [{"statement": 1, "quote": "We used 40 sensors"}]})
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


def test_a_fact_no_result_states_is_invented(runtime):
    grounded, invented = sme._content_grounded(
        "write the Introduction", "akinakes_publish_result", {"content": "Sensors were deployed in Kisumu county."},
        [('document_get({"doc_id": "d1"})', "A document about soil moisture, no place named.")], None,
    )
    assert not grounded and "Kisumu" in invented

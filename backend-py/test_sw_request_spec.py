"""The formalizer in three parts (app/sw_request_spec.py, app/microagents/sw_request_spec_parts.py): an action, condition
or outgoing message without the request's own words is refused; the answers merge into the old task text; a part that
fails adds nothing.
Run: python -m pytest test_sw_request_spec.py -q"""

import asyncio

import pytest

pytest.importorskip("claude_agent_sdk")
from app import microagent_client, sw_request_spec as rs  # noqa: E402
from app.microagents import sw_request_spec_parts as parts  # noqa: E402

REQUEST = "Set the objective of this project to: measure soil moisture with low-cost sensors across three test fields by March."
GIVEN = {"request": REQUEST, "dialogue": ""}


def work(actions, constraints=()):
    return {"requester": "tester", "goal": "set the objective", "actions": list(actions), "targets": [], "dates": ["by March"],
            "constraints": list(constraints)}


def test_an_action_the_request_does_not_ask_for_is_refused():
    # Live 2026-11-02: the single call added "create a formal project objective document".
    asked = {"do": "Set the project objective", "quote": "Set the objective of this project to"}
    invented = {"do": "Create a formal project objective document", "quote": "create a formal project objective document"}
    assert parts.verify_request_work(GIVEN, work([asked])) is None
    reason = parts.verify_request_work(GIVEN, work([asked, invented]))
    assert "not in the REQUEST" in reason and "Create a formal" in reason
    assert "non-empty" in parts.verify_request_work(GIVEN, work([{"do": "", "quote": "Set the objective"}]))
    assert parts.verify_request_work(GIVEN, work([asked], [{"condition": "by March", "quote": "by March"}])) is None
    assert parts.verify_request_work(GIVEN, work([asked], [{"condition": "cheap", "quote": "under 10 dollars"}]))


def test_a_quote_matches_despite_typography_and_may_come_from_the_conversation():
    given = {"request": 'Reply to him: "we will attend, two of us"', "dialogue": "Peter Harris: Will you come to the workshop?"}
    out = {"messages": [{"to": "Peter Harris", "says": "We will attend, two of us.", "quote": "Reply to him: «we will attend, two of us»"}]}
    assert parts.verify_request_outgoing(given, out) is None
    assert parts.verify_request_work(given, work([{"do": "answer Peter", "quote": "Will you come to the workshop?"}])) is None


def test_an_outgoing_message_nobody_asked_for_is_refused():
    # Live 2026-11-01: "tester: Your requested objective has been captured verbatim", posted and passed.
    invented = {"messages": [{"to": "tester", "says": "Your objective has been captured verbatim.", "quote": "captured verbatim"}]}
    assert "not in the REQUEST" in parts.verify_request_outgoing(GIVEN, invented)
    assert parts.verify_request_outgoing(GIVEN, {"messages": []}) is None
    assert 'needs "to"' in parts.verify_request_outgoing(GIVEN, {"messages": [{"to": "", "says": "x", "quote": "Set"}]})


def test_the_parts_merge_into_the_old_task_text():
    text = rs._render(rs.merge(
        work([{"do": "Set the project objective", "quote": "Set the objective"}]),
        {"messages": [{"to": "Peter", "says": "We will attend.", "quote": "x"}]},
        {"content": "the objective", "report": "whether the objective is set", "deliverable": "a project with its objective"},
    ))
    assert text.splitlines() == [
        "- Requester: tester",
        "- Goal: set the objective",
        "- Actions to take: Set the project objective",
        "- Content: the objective",
        "- Messages to send (to whom: exactly what it says): Peter: We will attend.",
        "- What to report to the user: whether the objective is set",
        "- Dates and deadlines: by March",
        "- What the user should end up with: a project with its objective",
    ]
    assert rs._render(rs.merge(None, None, None)) == ""


class FakeRuntime:
    def __init__(self, answers):
        self.answers = answers

    def register(self, agent):
        pass

    def new_context(self, budget=None):
        return None

    def run(self, name, given, context):
        answer = self.answers.get(name)
        return {"status": "ok", "output": answer} if answer is not None else {"status": "error", "error": {"code": "ladder_exhausted"}}


def test_formalize_runs_the_three_parts_and_a_failed_part_adds_nothing(monkeypatch):
    answers = {parts.REQUEST_WORK: work([{"do": "Set the project objective", "quote": "Set the objective"}]),
               parts.REQUEST_OUTCOME: {"content": "", "report": "whether the objective is set", "deliverable": ""}}
    monkeypatch.setattr(microagent_client, "build_local_runtime", lambda *a, **k: FakeRuntime(answers))
    monkeypatch.setattr(microagent_client, "new_budget", lambda: None)
    text = asyncio.run(rs.formalize(REQUEST, []))
    assert "- Actions to take: Set the project objective" in text and "Messages to send" not in text
    assert "- What to report to the user: whether the objective is set" in text
    monkeypatch.setattr(microagent_client, "build_local_runtime", lambda *a, **k: FakeRuntime({}))
    assert asyncio.run(rs.formalize(REQUEST, [])) is None

"""Akinakes Agent Bus channel (app/akinakes_channel.py): reply validation, the published envelope, and
system.skill_blocked for a turn that ended with no result.
Run: python -m pytest test_akinakes_channel.py -q"""

import asyncio

import pytest

from app import akinakes_channel as ch


class FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"messageId": "m-out"}


class FakeHttp:
    def __init__(self) -> None:
        self.posted: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict | None = None, **_kw) -> FakeResponse:
        self.posted.append((url, json))
        return FakeResponse()


def task(chain_id: str = "c1", task_type: str = "task.write_section") -> dict:
    msg = ch.make_message("coordinator", task_type, chain_id=chain_id, return_to="chatGateway", target_agent_uids=["writerAgent"], doc_id="d1", section_name="Intro")
    return msg


@pytest.fixture
def channel(monkeypatch):
    http = FakeHttp()
    monkeypatch.setattr(ch, "_config", ch.ChannelConfig("http://bus", "writerAgent", "writer", [], "", ""))
    monkeypatch.setattr(ch, "_http", http)
    monkeypatch.setattr(ch, "_pending", {})
    monkeypatch.setattr(ch, "_answered", set())
    monkeypatch.setattr(ch, "status", ch.ChannelStatus())
    return http


def run(coro):
    return asyncio.run(coro)


def test_load_config_needs_bus_and_uid(monkeypatch):
    monkeypatch.delenv("BUS_URL", raising=False)
    monkeypatch.delenv("AGENT_UID", raising=False)
    assert ch.load_config() is None
    monkeypatch.setenv("BUS_URL", "http://bus/")
    monkeypatch.setenv("AGENT_UID", "writerAgent")
    monkeypatch.setenv("AGENT_ROLE", "writer")
    config = ch.load_config()
    assert config is not None and config.bus_url == "http://bus" and config.role == "writer"


def test_chat_mode_defaults_to_sw_and_can_be_chosen(monkeypatch):
    monkeypatch.delenv("AKINAKES_CHAT_MODE", raising=False)
    assert ch.chat_mode() == "sw"
    monkeypatch.setenv("AKINAKES_CHAT_MODE", "Claude")
    assert ch.chat_mode() == "claude"
    monkeypatch.setenv("AKINAKES_CHAT_MODE", "gpt")
    assert ch.chat_mode() == "sw"


def test_result_is_a_reply_in_the_same_chain(channel):
    ch._pending["c1"] = task()
    run(ch.publish_result("c1", "result.section_draft", {"doc_id": "d1", "section_name": "Intro", "content": "Text"}))
    url, body = channel.posted[0]
    assert url == "http://bus/messages/publish"
    assert body["envelope"]["chainId"] == "c1"
    assert body["envelope"]["parentId"] == ch._pending["c1"]["envelope"]["messageId"]
    assert body["envelope"]["senderUid"] == "writerAgent"
    assert body["envelope"]["routing"]["returnTo"] == "chatGateway"
    assert body["payload"] == {"type": "result.section_draft", "doc_id": "d1", "section_name": "Intro", "content": "Text"}


def test_wrong_type_or_missing_field_is_refused_and_nothing_is_published(channel):
    ch._pending["c1"] = task()
    with pytest.raises(ValueError, match="payload_type must be"):
        run(ch.publish_result("c1", "result.article_draft", {"doc_id": "d1", "sections": []}))
    with pytest.raises(ValueError, match="Missing fields"):
        run(ch.publish_result("c1", "result.section_draft", {"doc_id": "d1"}))
    with pytest.raises(ValueError, match="No task is in progress"):
        run(ch.publish_result("other", "result.section_draft", {}))
    assert channel.posted == []


def test_turn_ending_without_a_result_publishes_skill_blocked(channel):
    ch._pending["c1"] = task()
    run(ch.on_turn_end("c1"))
    _url, body = channel.posted[0]
    assert body["payload"]["type"] == "system.skill_blocked"
    assert body["payload"]["skill"] == "write_section"
    assert body["envelope"]["routing"]["targetAgentUids"] == ["chatGateway"]
    assert "c1" not in ch._pending


def test_turn_ending_after_a_result_publishes_nothing_more(channel):
    ch._pending["c1"] = task()
    run(ch.publish_result("c1", "result.section_draft", {"doc_id": "d1", "section_name": "Intro", "content": "Text"}))
    run(ch.on_turn_end("c1"))
    assert len(channel.posted) == 1


def test_task_text_names_the_chain_and_the_expected_reply():
    text = ch.render_task_text(task("c9", "task.write_sections_batch"))
    assert 'chainId="c9"' in text
    assert 'payload_type="result.article_draft"' in text and "doc_id, sections" in text
    assert "not found" in text and "report it blocked at once" in text

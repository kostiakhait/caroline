"""Akinakes Agent Bus channel (app/akinakes_channel.py): reply validation, the published envelope, the blocked reply of
a turn that ended with no result, and chat messages (the intake gate, answering in the room, agent.pass).
Run: python -m pytest test_akinakes_channel.py -q"""

import asyncio

import pytest

from app import akinakes_channel as ch


class FakeResponse:
    def __init__(self, data=None) -> None:
        self.data = {"messageId": "m-out"} if data is None else data

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self.data


class FakeHttp:
    def __init__(self, answers: dict | None = None) -> None:
        self.posted: list[tuple[str, dict]] = []
        self.answers = answers or {}  # url -> response data

    async def post(self, url: str, json: dict | None = None, **_kw) -> FakeResponse:
        self.posted.append((url, json))
        return FakeResponse(self.answers.get(url))

    async def get(self, url: str, params: dict | None = None, **_kw) -> FakeResponse:
        return FakeResponse(self.answers.get(url))


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


def edit_task(chain_id: str = "e1", **payload) -> dict:
    return ch.make_message("chatSecretary", "task.edit_section", chain_id=chain_id, return_to="chatSecretary",
                           target_agent_uids=["editorAgent"], **{"doc_id": "d1", "room_id": "ws-default/r1", **payload})


def test_a_task_with_two_kinds_of_reply_accepts_either_and_echoes_the_room(channel):
    ch._pending["e1"] = edit_task(section_address="the intro", edit_instruction="shorter")
    run(ch.publish_result("e1", "result.edit_section_done", {"doc_id": "d1", "section_id": "s1", "section_address": "the intro"}))
    assert channel.posted[0][1]["payload"]["room_id"] == "ws-default/r1"
    ch._pending["e2"] = edit_task("e2", section_id="s1", content="old")
    run(ch.publish_result("e2", "result.edit_proposed", {"doc_id": "d1", "section_id": "s1", "edit_id": "x", "content": "new"}))
    with pytest.raises(ValueError, match="one of: result.edit_section_done, result.edit_proposed"):
        run(ch.publish_result("e2", "result.section_draft", {}))
    text = ch.render_task_text(edit_task())
    assert "one of:" in text and "section_address" in text and "document_propose_edit" in text


def test_a_task_with_its_own_blocked_reply_gets_it_instead_of_skill_blocked(channel):
    ch._pending["e1"] = edit_task()
    run(ch.on_turn_end("e1"))
    payload = channel.posted[0][1]["payload"]
    assert payload == {"type": "result.edit_section_done", "room_id": "ws-default/r1",
                       "error": "The agent's turn ended without publishing a result."}
    ch._pending["p1"] = ch.make_message("coordinator", "task.create_experiment_plan_doc", chain_id="p1", objective="x")
    run(ch.publish_blocked("p1", "Document Service unavailable"))
    assert channel.posted[1][1]["payload"] == {"type": "result.plan_error", "reason": "Document Service unavailable"}


def chat(chain_id: str = "m1", room: str = "ws-default/r1", sender: str = "alice", text: str = "Shorten the intro", to=None) -> dict:
    msg = ch.make_message("chatGateway", "chat.message", chain_id=chain_id, target_agent_uids=to or [], room_id=room, text=text)
    msg["payload"]["sender_uid"] = sender  # a payload field of chat.message, not make_message's own sender_uid
    return msg


def chat_config(**kw) -> "ch.ChannelConfig":
    return ch.ChannelConfig("http://bus", "editorAgent", "editor", [], "", "", chat_url="http://chat", chat_key="k", **kw)


def test_a_chat_message_is_answered_in_its_room_and_a_silent_turn_passes(channel, monkeypatch):
    monkeypatch.setattr(ch, "_config", chat_config())
    ch._pending["m1"] = chat()
    run(ch.post_chat("m1", "Done: the intro is two sentences now."))
    url, body = channel.posted[0]
    assert url == "http://chat/internal/rooms/ws-default/r1/agent_message"
    assert body["payload"] == {"text": "Done: the intro is two sentences now."} and body["sender_uid"] == "editorAgent"
    run(ch.on_turn_end("m1"))
    assert len(channel.posted) == 1  # answered: no pass
    ch._pending["m2"] = chat("m2")
    run(ch.on_turn_end("m2"))
    payload = channel.posted[1][1]
    assert payload["payload"] == {"type": "agent.pass", "room_id": "ws-default/r1", "agent_uid": "editorAgent"}
    assert payload["envelope"]["routing"]["targetAgentUids"] == ["chatSecretary"]
    with pytest.raises(ValueError, match="answered in its room"):
        ch._pending["m3"] = chat("m3")
        run(ch.publish_result("m3", "result.section_draft", {}))


def test_the_intake_gate_checks_scope_then_acl(monkeypatch):
    acl = FakeHttp({"http://acl/check": {"allowed": False}})
    open_acl = FakeHttp({"http://acl/check": {"allowed": True}})
    config = chat_config(scope=[{"room_id": "ws-default", "recursive": True}], acl_url="http://acl")
    assert run(ch.admits_chat_message(chat(), config, open_acl))
    assert not run(ch.admits_chat_message(chat(room="other-ws/r1"), config, open_acl))  # out of scope
    assert run(ch.admits_chat_message(chat(room="other-ws/r1", to=["editorAgent"]), config, open_acl))  # addressed: scope skipped
    assert not run(ch.admits_chat_message(chat(to=["editorAgent"]), config, acl))  # ACL always applies
    assert not run(ch.admits_chat_message(chat(sender="editorAgent"), config, open_acl))  # its own message
    assert not run(ch.admits_chat_message(chat(text="  "), config, open_acl))
    assert acl.posted[0][1] == {"subject": "alice", "object_type": "agent", "object_id": "editorAgent", "operation": "use"}


def test_scope_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_SCOPE", '[{"room_id": "ws-default", "recursive": true}]')
    assert ch._scope_from_env() == [{"room_id": "ws-default", "recursive": True}]
    monkeypatch.setenv("AGENT_SCOPE", "not json")
    assert ch._scope_from_env() == []

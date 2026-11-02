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
    # ...but only for what has to exist already: a section to write is new (the second real run's batch was refused).
    assert "A section the task asks you to write may not exist yet" in text
    # The task's type is not a tool (the formalized request once read "invoke task.write_sections_batch").
    assert '"task.write_sections_batch" is the kind of task, not a tool' in text


def edit_task(chain_id: str = "e1", **payload) -> dict:
    return ch.make_message("chatSecretary", "task.edit_section", chain_id=chain_id, return_to="chatSecretary",
                           target_agent_uids=["editorAgent"], **{"doc_id": "d1", "room_id": "ws-default/r1", **payload})


def test_a_task_with_two_kinds_of_reply_accepts_either_and_echoes_the_room(channel):
    ch._pending["e1"] = edit_task(section_address="the intro", edit_instruction="shorter")
    run(ch.publish_result("e1", "result.edit_section_done", {"doc_id": "d1", "section_id": "s1", "section_address": "the intro"}))
    assert channel.posted[0][1]["payload"]["room_id"] == "ws-default/r1"
    ch._pending["e2"] = edit_task("e2", section_id="s1", content="old")
    run(ch.publish_result("e2", "result.edit_proposed", {"doc_id": "d1", "section_id": "s1", "edit_id": "x", "content": "new"}))
    ch._pending["e3"] = edit_task("e3", section_id="s1", content="old")
    with pytest.raises(ValueError, match="one of: result.edit_section_done, result.edit_proposed"):
        run(ch.publish_result("e3", "result.section_draft", {}))
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


def orchestrator(monkeypatch, answers: dict | None = None) -> FakeHttp:
    http = FakeHttp(answers)
    monkeypatch.setattr(ch, "_config", ch.ChannelConfig("http://bus", "chatSecretary", "chat_secretary", [], "", "", chat_url="http://chat", chat_key="k", orchestrator=True))
    monkeypatch.setattr(ch, "_http", http)
    monkeypatch.setattr(ch, "_pending", {"m1": chat()})
    monkeypatch.setattr(ch, "_answered", set())
    monkeypatch.setattr(ch, "_asked", {})
    monkeypatch.setattr(ch, "_passes", {})
    monkeypatch.setattr(ch, "_relay_keys", set())
    monkeypatch.setattr(ch, "_posted", set())
    monkeypatch.setattr(ch, "_bus_tasks", {})
    return http


def test_the_coordinator_says_one_thing_per_request(monkeypatch):
    http = orchestrator(monkeypatch)
    run(ch.post_chat("m1", "Hello!"))
    with pytest.raises(ValueError, match="already has your answer"):
        run(ch.post_chat("m1", "Hello again!"))
    assert len(http.posted) == 1
    run(ch.on_turn_end("m1"))
    assert "m1" not in ch._posted
    text = ch.render_task_text(chat())
    assert "wrote in the chat room" in text and "Shorten the intro" in text and 'chainId "m1"' in text


def test_orchestrator_tools_refuse_a_role_that_is_not_the_coordinator(channel):
    with pytest.raises(ValueError, match="chat coordinator only"):
        run(ch.room_agents("m1"))


def test_room_agents_lists_agent_members_except_itself(monkeypatch):
    members = [{"uid": "alice", "ptype": "human"}, {"uid": "editorAgent", "ptype": "agent"}, {"uid": "chatSecretary", "ptype": "agent"}]
    room = {"id": "ws-default/r1", "name": "Thermal Research", "room_type": "project", "parent_id": "ws-default"}
    orchestrator(monkeypatch, {"http://chat/rooms/ws-default/r1/members": members, "http://chat/rooms/ws-default/r1": room})
    result = run(ch.room_agents("m1"))
    assert [a["uid"] for a in result["agents"]] == ["editorAgent"]
    assert result["room"] == {"id": "ws-default/r1", "name": "Thermal Research", "type": "project", "parent_id": "ws-default"}


ROOM_MEMBERS = {"http://chat/rooms/ws-default/r1/members": [
    {"uid": "alice", "ptype": "human"}, {"uid": "editorAgent", "ptype": "agent"}, {"uid": "researchAgent", "ptype": "agent"},
    {"uid": "chatSecretary", "ptype": "agent"},
]}


def test_hand_to_agents_publishes_the_message_addressed_to_each(monkeypatch):
    http = orchestrator(monkeypatch, ROOM_MEMBERS)
    sent = run(ch.hand_to_agents("m1", ["editorAgent", "researchAgent"], None))
    assert sent == ["editorAgent", "researchAgent"]
    first, second = (body for _url, body in http.posted)
    assert first["envelope"]["routing"]["targetAgentUids"] == ["editorAgent"]
    assert second["envelope"]["routing"]["targetAgentUids"] == ["researchAgent"]
    assert first["payload"]["type"] == "chat.message" and first["payload"]["room_id"] == "ws-default/r1"
    assert first["payload"]["sender_uid"] == "alice" and first["payload"]["text"] == "Shorten the intro"
    assert ch._asked["m1"] == {"editorAgent", "researchAgent"} and "m1" in ch._answered
    with pytest.raises(ValueError, match="empty"):
        run(ch.hand_to_agents("m1", [], None))
    with pytest.raises(ValueError, match="already handed"):
        run(ch.hand_to_agents("m1", ["editorAgent"], None))
    assert len(http.posted) == 2


def test_hand_to_agents_refuses_uids_that_are_not_agents_of_the_room(monkeypatch):
    # Live 2026-11-01: invented uids agent-01..03, published to nobody.
    http = orchestrator(monkeypatch, ROOM_MEMBERS)
    with pytest.raises(ValueError, match="Not agents of this room, nothing was sent: agent-01, alice") as refused:
        run(ch.hand_to_agents("m1", ["editorAgent", "agent-01", "alice"], None))
    assert "The room's agents are: editorAgent, researchAgent" in str(refused.value)
    assert http.posted == [] and "m1" not in ch._asked and "m1" not in ch._answered
    with pytest.raises(ValueError, match="Not agents of this room"):
        run(ch.hand_to_agents("m1", ["chatSecretary"], None))  # not to itself either


def test_hand_to_agents_sends_nothing_when_the_room_cannot_be_read(monkeypatch):
    http = orchestrator(monkeypatch)  # no member list: Chat Service's answer is unusable
    with pytest.raises(ValueError, match="Could not read the room's members, nothing was sent"):
        run(ch.hand_to_agents("m1", ["editorAgent"], None))
    assert http.posted == []


REGISTRY = [
    {"uid": "projectSecretary", "role": "secretary", "accepts": [{"type": "task.project_set_objective", "description": "Set a project's objective.",
                                                                  "fields": {"project_id": "the project (its room id)", "objective": "the objective"}}]},
    {"uid": "editorAgent", "role": "editor", "accepts": []},
    {"uid": "chatSecretary", "role": "chat_secretary", "accepts": [{"type": "task.x", "fields": {}}]},
]


def with_registry(monkeypatch) -> FakeHttp:
    http = orchestrator(monkeypatch, {"http://registry/agents": REGISTRY})
    ch._config.registry_url = "http://registry"
    return http


def test_a_bus_task_must_be_a_task_or_query(monkeypatch):
    http = orchestrator(monkeypatch)
    with pytest.raises(ValueError, match="task_type must start"):
        run(ch.publish_bus_task("m1", "projectSecretary", "result.x", {}))
    run(ch.publish_bus_task("m1", "projectSecretary", "task.project_set_objective", {"project_id": "p1", "objective": "Ship it"}))
    payload = http.posted[0][1]["payload"]
    assert payload["type"] == "task.project_set_objective" and payload["objective"] == "Ship it" and payload["room_id"] == "ws-default/r1"


def test_a_bus_task_goes_to_the_chosen_agent_and_comes_back_here(monkeypatch):
    """Without a route the bus lets any poller claim the task, the coordinator itself included."""
    http = with_registry(monkeypatch)
    run(ch.publish_bus_task("m1", "projectSecretary", "task.project_set_objective", {"project_id": "ws-default/r1", "objective": "Ship it"}))
    routing = http.posted[0][1]["envelope"]["routing"]
    assert routing["route"] == ["projectSecretary"] and routing["returnTo"] == "chatSecretary"
    assert http.posted[0][1]["envelope"]["chainId"] == "m1"
    assert ch._bus_tasks == {"m1": "ws-default/r1"}


def test_a_bus_task_is_checked_against_what_the_agent_declares(monkeypatch):
    http = with_registry(monkeypatch)
    with pytest.raises(ValueError, match="does not take task.write_section; it takes: task.project_set_objective"):
        run(ch.publish_bus_task("m1", "projectSecretary", "task.write_section", {}))
    with pytest.raises(ValueError, match='No agent "ghostAgent"'):
        run(ch.publish_bus_task("m1", "ghostAgent", "task.project_set_objective", {}))
    with pytest.raises(ValueError, match="agent_uid must be"):
        run(ch.publish_bus_task("m1", "chatSecretary", "task.x", {}))
    assert http.posted == []


def test_bus_agents_lists_the_agents_that_declare_tasks_except_itself(monkeypatch):
    with_registry(monkeypatch)
    agents = run(ch.bus_agents("m1"))
    assert [a["uid"] for a in agents] == ["projectSecretary"]
    assert agents[0]["accepts"][0]["fields"]["project_id"] == "the project (its room id)"


class PollOnce(FakeHttp):
    """Answers the first bus poll with these messages, then stops the loop."""

    def __init__(self, messages: list[dict]) -> None:
        super().__init__()
        self.messages = messages

    async def post(self, url: str, json: dict | None = None, **_kw) -> FakeResponse:
        if url.endswith("/messages/poll"):
            if self.messages is None:
                raise asyncio.CancelledError
            batch, self.messages = self.messages, None
            return FakeResponse({"messages": [{"message": m} for m in batch]})
        return await super().post(url, json)


def test_the_result_of_a_published_task_is_told_in_its_room_even_without_room_id(monkeypatch):
    """The secretaries' result.secretary_ok carries no room_id; the room comes from the published task."""
    orchestrator(monkeypatch)
    ch._bus_tasks["m1"] = "ws-default/r1"
    result = ch.make_message("projectSecretary", "result.secretary_ok", chain_id="m1", project_id="ws-default/r1", objective="Ship it")
    injected = []
    with pytest.raises(asyncio.CancelledError):
        run(ch._poll_forever(PollOnce([result]), ch._config, lambda key, text, request: injected.append((key, text)), None))
    (key, text), = injected
    assert key.startswith("m1-relay-") and key in ch._relay_keys
    assert ch._pending[key]["payload"]["room_id"] == "ws-default/r1" and "result.secretary_ok" in text
    assert ch._bus_tasks == {}


def test_a_role_declares_its_tasks_from_the_reply_specs(monkeypatch):
    config = ch.ChannelConfig("http://bus", "projectSecretary", "secretary", [], "", "http://registry",
                              accepts=["task.project_set_objective", "query.project_state"])
    declared = ch.accepted_messages(config)
    assert [a["type"] for a in declared] == ["task.project_set_objective", "query.project_state"]
    assert declared[0]["description"] and "project_id" in declared[0]["fields"]

    class Registry(FakeHttp):
        def __init__(self, code: int) -> None:
            super().__init__()
            self.code, self.patched = code, []

        async def post(self, url, json=None, **_kw):
            self.posted.append((url, json))
            resp = FakeResponse({"token": "t"})
            resp.status_code = self.code
            return resp

        async def patch(self, url, json=None, **_kw):
            self.patched.append((url, json))
            return FakeResponse()

    fresh = Registry(201)
    run(ch._register(fresh, config))
    assert fresh.posted[0][1]["accepts"] == declared and fresh.patched == []
    again = Registry(409)  # the old agent of this uid registered it first
    run(ch._register(again, config))
    assert again.patched == [("http://registry/agents/projectSecretary", {"accepts": declared})]


def test_wait_for_agent_replies_reports_replies_passes_and_silence(monkeypatch):
    reply = {"sender_type": "agent", "sender_uid": "editorAgent", "payload": {"text": "Done"}}
    orchestrator(monkeypatch, {"http://chat/rooms/ws-default/r1/messages": [reply]})
    ch._asked["m1"] = {"editorAgent", "researchAgent"}
    ch._passes["m1"] = {"researchAgent"}
    result = run(ch.wait_for_agent_replies("m1", 1))
    assert [m["sender_uid"] for m in result["replies"]] == ["editorAgent"]
    assert result["passed"] == ["researchAgent"] and result["silent"] == []


def test_the_coordinator_ignores_messages_agents_sent(monkeypatch):
    orchestrator(monkeypatch)
    agent_msg = chat()
    agent_msg["payload"]["sender_type"] = "agent"
    config = ch._config
    assert not run(ch.admits_chat_message(agent_msg, config, FakeHttp()))
    assert run(ch.admits_chat_message(chat(), config, FakeHttp()))


def test_a_coordinator_turn_without_an_answer_tells_the_room_and_a_relay_turn_does_not(monkeypatch):
    http = orchestrator(monkeypatch)
    run(ch.on_turn_end("m1"))
    assert http.posted[0][0] == "http://chat/internal/rooms/ws-default/r1/agent_message"
    ch._pending["m1-relay-ab"] = chat("m1-relay-ab")
    ch._relay_keys.add("m1-relay-ab")
    run(ch.on_turn_end("m1-relay-ab"))
    assert len(http.posted) == 1


def test_scope_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_SCOPE", '[{"room_id": "ws-default", "recursive": true}]')
    assert ch._scope_from_env() == [{"room_id": "ws-default", "recursive": True}]
    monkeypatch.setenv("AGENT_SCOPE", "not json")
    assert ch._scope_from_env() == []


def test_fields_must_follow_the_reply_schema(channel):
    # The second real run published bare section names; Human Gate reads section["name"] and section["content"].
    ch._pending["c1"] = task("c1", "task.write_sections_batch")
    with pytest.raises(ValueError, match="sections/[01]: .* is not of type .object."):
        run(ch.publish_result("c1", "result.article_draft", {"doc_id": "d1", "sections": ["Introduction", "Discussion"]}))
    with pytest.raises(ValueError, match="'name' is a required property"):
        run(ch.publish_result("c1", "result.article_draft", {"doc_id": "d1", "sections": [{"title": "Intro", "content": "x"}]}))
    assert channel.posted == []
    run(ch.publish_result("c1", "result.article_draft", {"doc_id": "d1", "sections": [{"name": "Intro", "content": "Text"}]}))
    assert "fields JSON schema" in ch.render_task_text(task("c2", "task.write_sections_batch"))


def test_a_task_is_answered_once(channel):
    ch._pending["c1"] = task()
    run(ch.publish_result("c1", "result.section_draft", {"doc_id": "d1", "section_name": "Intro", "content": "Text"}))
    with pytest.raises(ValueError, match="already answered"):
        run(ch.publish_blocked("c1", "no tool"))
    with pytest.raises(ValueError, match="already answered"):
        run(ch.publish_result("c1", "result.section_draft", {"doc_id": "d1", "section_name": "Intro", "content": "Again"}))
    assert len(channel.posted) == 1


def test_the_requester_words_are_the_message_or_the_task_without_our_instructions():
    words = ch.requester_words(chat())
    assert words == "alice wrote in the chat room:\nShorten the intro"
    words = ch.requester_words(task("c1", "task.write_section"))
    assert words.startswith("coordinator sent the task task.write_section over the Agent Bus, with:")
    assert '"section_name": "Intro"' in words and "akinakes_publish_result" not in words


def test_the_channel_injects_a_task_with_its_requester_words(monkeypatch, channel):
    injected = []
    with pytest.raises(asyncio.CancelledError):
        run(ch._poll_forever(PollOnce([task("c7", "task.write_section")]), ch._config,
                             lambda key, text, request: injected.append((key, text, request)), None))
    key, text, request = injected[0]
    assert key == "c7" and "akinakes_publish_result" in text and request == ch.requester_words(task("c7", "task.write_section"))

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
    assert "wrote in the chat room" in text and "Shorten the intro" in text and "need no chainId" in text


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
    {"uid": "projectSecretary", "role": "secretary", "accepts": [{"type": "task.project_record_note", "description": "Record a note for a project.",
                                                                  "fields": {"project_id": "the project (its room id)", "note": "the note"}}]},
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
    run(ch.publish_bus_task("m1", "projectSecretary", "task.project_record_note", {"project_id": "p1", "note": "Ship it"}))
    payload = http.posted[0][1]["payload"]
    assert payload["type"] == "task.project_record_note" and payload["note"] == "Ship it" and payload["room_id"] == "ws-default/r1"


def test_a_bus_task_goes_to_the_chosen_agent_and_comes_back_here(monkeypatch):
    """Without a route the bus lets any poller claim the task, the coordinator itself included."""
    http = with_registry(monkeypatch)
    run(ch.publish_bus_task("m1", "projectSecretary", "task.project_record_note", {"project_id": "ws-default/r1", "note": "Ship it"}))
    routing = http.posted[0][1]["envelope"]["routing"]
    assert routing["route"] == ["projectSecretary"] and routing["returnTo"] == "chatSecretary"
    assert http.posted[0][1]["envelope"]["chainId"] == "m1"
    assert ch._bus_tasks == {"m1": {"room_id": "ws-default/r1", "asked_by": "alice", "asked": "Shorten the intro"}}


def test_a_bus_task_may_name_its_own_room_among_its_fields(monkeypatch):
    # Live 2026-11-02: docCreator's task has a room_id field, and the call crashed with "multiple values for room_id".
    http = with_registry(monkeypatch)
    run(ch.publish_bus_task("m1", "projectSecretary", "task.project_record_note",
                            {"project_id": "p1", "note": "Ship it", "room_id": "other-room", "user_uid": "mallory"}))
    payload = http.posted[0][1]["payload"]
    assert payload["room_id"] == "other-room" and payload["note"] == "Ship it"
    assert payload["user_uid"] == "alice" and payload["original_text"] == "Shorten the intro"  # always from the message
    assert ch._bus_tasks["m1"]["room_id"] == "ws-default/r1"  # the result is still told in the message's room


def test_a_bus_task_is_checked_against_what_the_agent_declares(monkeypatch):
    http = with_registry(monkeypatch)
    with pytest.raises(ValueError, match="does not take task.write_section; it takes: task.project_record_note"):
        run(ch.publish_bus_task("m1", "projectSecretary", "task.write_section", {}))
    with pytest.raises(ValueError, match='No agent "ghostAgent"'):
        run(ch.publish_bus_task("m1", "ghostAgent", "task.project_record_note", {}))
    with pytest.raises(ValueError, match="to_agent is the agent that takes this task, not you"):
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
    ch._bus_tasks["m1"] = {"room_id": "ws-default/r1", "asked_by": "alice", "asked": "Set the objective to: ship it"}
    result = ch.make_message("projectSecretary", "result.secretary_ok", chain_id="m1", project_id="ws-default/r1", objective="Ship it")
    injected = []
    with pytest.raises(asyncio.CancelledError):
        run(ch._poll_forever(PollOnce([result]), ch._config, lambda key, text, request, relay=False: injected.append((key, text, request, relay)), None))
    (key, text, request, relay), = injected
    # A relay turn is not a request: the turn gets what the person asked and what the agent answered, not our template,
    # and the relay flag (no formalizer, no start-rung advice).
    assert relay is True
    words = ch.relay_words(ch._pending[key], {"asked_by": "alice", "asked": "Set the objective to: ship it"})
    assert request == words
    assert words.startswith("alice asked in the chat room:\nSet the objective to: ship it\n\nprojectSecretary answered result.secretary_ok")
    assert '"objective": "Ship it"' in words and "Agent Bus result" not in words
    assert key.startswith("m1-relay-") and key in ch._relay_keys
    assert ch._pending[key]["payload"]["room_id"] == "ws-default/r1" and "result.secretary_ok" not in text and "Ship it" in text
    assert ch._bus_tasks == {}


def test_a_role_declares_its_tasks_from_the_reply_specs(monkeypatch):
    config = ch.ChannelConfig("http://bus", "projectSecretary", "secretary", [], "", "http://registry",
                              accepts=["task.level_request", "query.project_state"])
    declared = ch.accepted_messages(config)
    assert [a["type"] for a in declared] == ["task.level_request", "query.project_state"]
    assert declared[0]["description"] and "text" in declared[0]["fields"] and "project_id" in declared[1]["fields"]

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


def test_a_coordinator_turn_with_nothing_for_the_room_gets_one_more_turn_then_the_fallback(monkeypatch):
    http = orchestrator(monkeypatch)
    injected = []
    monkeypatch.setattr(ch, "_inject", lambda key, text, words, relay=False: injected.append((key, text, words, relay)))
    monkeypatch.setattr(ch, "_nudged", set())
    run(ch.on_turn_end("m1"))
    assert http.posted == [] and "m1" in ch._pending and injected[0][0] == "m1" and "hand_to_agents" in injected[0][1]
    assert injected[0][3] is True  # the coordinator's nudge is not formalized either
    assert "Shorten the intro" in injected[0][2]
    run(ch.on_turn_end("m1"))
    assert len(injected) == 1 and http.posted[0][0] == "http://chat/internal/rooms/ws-default/r1/agent_message"
    assert "m1" not in ch._pending and "m1" not in ch._nudged


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
    assert words == "Shorten the intro"
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


def test_the_coordinator_ignores_events_on_the_bus(monkeypatch):
    orchestrator(monkeypatch)
    monkeypatch.setattr(ch, "_registry_agents", lambda: asyncio.sleep(0, {}))
    injected = []
    event = ch.make_message("documentService", "event.section_written", chain_id="e1", doc_id="d1")
    with pytest.raises(asyncio.CancelledError):
        run(ch._poll_forever(PollOnce([event]), ch._config,
                             lambda key, text, request, relay=False: injected.append(key), None))
    assert injected == [] and "e1" not in ch._pending


def test_only_the_coordinator_s_chat_turns_skip_the_formalizer(monkeypatch):
    """The coordinator routes, it does not do the work: its chat turns are relay turns (live 2026-11-05: the formalizer
    asked for the person's own words to be posted back). Every other role's turn is injected as before."""
    orchestrator(monkeypatch)
    monkeypatch.setattr(ch, "_registry_agents", lambda: asyncio.sleep(0, {}))
    injected = []
    with pytest.raises(asyncio.CancelledError):
        run(ch._poll_forever(PollOnce([chat("m9")]), ch._config,
                             lambda key, text, request, relay=False: injected.append((key, request, relay)), None))
    assert injected and injected[0][2] is True and injected[0][1] == "Shorten the intro" + chr(10) * 2 + ch.ROOM_SECRETARY_TASK



def test_a_tool_takes_the_chain_of_its_session_not_the_model_s_copy(monkeypatch):
    """Live 2026-11-02: the model copied the chainId with one digit wrong, and every tool refused."""
    from app import session_context
    token = session_context.set_tab_id("akinakes-3c2474d8-fefb-4099-a70c-8dd45369de1f")
    try:
        assert ch.session_chain_id("3c2474d8-fefb-4096-a70c-8dd45369de1f") == "3c2474d8-fefb-4099-a70c-8dd45369de1f"
        assert ch.session_chain_id("") == "3c2474d8-fefb-4099-a70c-8dd45369de1f"
    finally:
        session_context._current_tab_id.reset(token)
    assert ch.session_chain_id("c1") == "c1"  # outside an akinakes session, as given


# --- broadcast requests and the level secretaries (akinakes plan, group 3, 2026-11-05) --------------------------------

LEVEL_REGISTRY = [
    {"uid": "projectSecretary", "role": "secretary", "accepts": [{"type": "task.level_request", "fields": {}}]},
    {"uid": "writerAgent", "role": "writer", "accepts": [{"type": "task.write_section", "fields": {}}]},
    {"uid": "chatSecretary", "role": "chat_secretary", "accepts": []},
]


def test_level_secretary_switch_from_env(monkeypatch):
    monkeypatch.setenv("BUS_URL", "http://bus")
    monkeypatch.setenv("AGENT_UID", "projectSecretary")
    monkeypatch.delenv("AKINAKES_LEVEL_SECRETARY", raising=False)
    assert ch.load_config().level == ""
    monkeypatch.setenv("AKINAKES_LEVEL_SECRETARY", "Project")
    assert ch.load_config().level == "project"
    monkeypatch.setenv("AKINAKES_LEVEL_SECRETARY", "team")
    assert ch.load_config().level == ""


def test_a_task_without_an_addressee_is_broadcast(monkeypatch):
    http = orchestrator(monkeypatch, {"http://registry/agents": LEVEL_REGISTRY})
    ch._config.registry_url = "http://registry"
    monkeypatch.setattr(ch, "_claimed", set())
    run(ch.publish_bus_task("m1", "", "task.level_request", {}))
    body = http.posted[0][1]
    routing = body["envelope"]["routing"]
    # no route and no returnTo: the bus gives it to every poller (with returnTo it would give it only to that agent)
    assert routing["route"] == [] and routing["returnTo"] is None and routing["targetAgentUids"] == []
    assert body["payload"]["text"] == "Shorten the intro" and body["payload"]["room_id"] == "ws-default/r1"
    assert body["payload"]["asked_by"] == "alice"
    assert ch._bus_tasks["m1"]["room_id"] == "ws-default/r1" and "m1" in ch._answered
    with pytest.raises(ValueError, match="already on the bus"):
        run(ch.publish_bus_task("m1", "", "task.level_request", {}))


def test_a_broadcast_needs_some_agent_that_takes_its_type(monkeypatch):
    http = orchestrator(monkeypatch, {"http://registry/agents": LEVEL_REGISTRY})
    ch._config.registry_url = "http://registry"
    with pytest.raises(ValueError, match="No agent on the bus takes task.nothing"):
        run(ch.publish_bus_task("m1", "", "task.nothing", {}))
    assert http.posted == []


def test_nobody_claims_a_broadcast_and_the_room_is_told(monkeypatch):
    orchestrator(monkeypatch)
    injected = []
    monkeypatch.setattr(ch, "_inject", lambda key, text, request, relay=False: injected.append((key, text, request, relay)))
    monkeypatch.setattr(ch, "_claimed", set())
    monkeypatch.setattr(ch, "CLAIM_WAIT_S", 0)
    ch._bus_tasks["m1"] = {"room_id": "ws-default/r1", "asked_by": "alice", "asked": "Set the objective"}
    run(ch._claim_deadline("m1"))
    (key, text, request, relay), = injected
    assert relay is True and ch._pending[key]["payload"]["type"] == ch.NOT_TAKEN
    assert ch._pending[key]["payload"]["room_id"] == "ws-default/r1" and "No agent on the bus took" in text
    assert "alice asked in the chat room:\nSet the objective" in request and ch._bus_tasks == {}


def test_a_claimed_broadcast_is_not_reported_as_not_taken(monkeypatch):
    orchestrator(monkeypatch)
    injected = []
    monkeypatch.setattr(ch, "_inject", lambda *a, **k: injected.append(a))
    monkeypatch.setattr(ch, "_claimed", set())
    monkeypatch.setattr(ch, "CLAIM_WAIT_S", 0)
    ch._bus_tasks["m1"] = {"room_id": "ws-default/r1", "asked_by": "alice", "asked": "x"}
    claim = ch.make_message("projectSecretary", ch.LEVEL_CLAIMED, chain_id="m1", target_agent_uids=["chatSecretary"])
    with pytest.raises(asyncio.CancelledError):
        run(ch._poll_forever(PollOnce([claim]), ch._config, lambda *a, **k: injected.append(a), None))
    assert "m1" in ch._claimed and injected == []  # the claim is not a task for the coordinator
    run(ch._claim_deadline("m1"))
    assert injected == [] and "m1" in ch._bus_tasks  # the answer is still awaited


def level_request(chain_id: str = "L1", text: str = "Set the objective of this project to shipping v2") -> dict:
    return ch.make_message("chatSecretary", "task.level_request", chain_id=chain_id, text=text, room_id="p1", asked_by="alice")


def test_a_worker_takes_only_broadcasts_of_the_types_it_declares_and_never_its_own(monkeypatch, channel):
    monkeypatch.setattr(ch, "_config", ch.ChannelConfig("http://bus", "writerAgent", "writer", [], "", "", accepts=["task.write_section"]))
    own = ch.make_message("writerAgent", "task.write_section", chain_id="own", doc_id="d1")
    broadcast_write = ch.make_message("chatSecretary", "task.write_section", chain_id="w1", doc_id="d1", section_name="Intro")
    addressed_other = ch.make_message("chatSecretary", "task.edit_section", chain_id="a1", route=["writerAgent"])
    injected = []
    with pytest.raises(asyncio.CancelledError):
        run(ch._poll_forever(PollOnce([level_request(), own, broadcast_write, addressed_other]), ch._config,
                             lambda key, text, request, relay=False: injected.append(key), None))
    assert injected == ["w1", "a1"]


def test_a_role_that_declares_nothing_still_takes_every_task(monkeypatch, channel):
    """Unchanged default: the type check applies only to a role that declares its types."""
    injected = []
    with pytest.raises(asyncio.CancelledError):
        run(ch._poll_forever(PollOnce([level_request()]), ch._config, lambda key, *a, **k: injected.append(key), None))
    assert injected == ["L1"]


def test_a_reply_to_a_routed_task_is_unchanged_by_the_sender_option():
    routed = task()
    assert ch.make_reply(routed, "w", "result.x", to_sender_if_unrouted=True)["envelope"]["routing"]["targetAgentUids"] == []
    unrouted = level_request()
    assert ch.make_reply(unrouted, "w", "result.x")["envelope"]["routing"]["targetAgentUids"] == []  # default: as before
    assert ch.make_reply(unrouted, "w", "result.x", to_sender_if_unrouted=True)["envelope"]["routing"]["targetAgentUids"] == ["chatSecretary"]


ROOMS = [
    {"id": "ws1", "name": "Research Lab", "room_type": "workspace", "parent_id": None},
    {"id": "p1", "name": "Thermal Research", "room_type": "project", "parent_id": "ws1"},
    {"id": "p2", "name": "Report Writing", "room_type": "project", "parent_id": "ws1"},
    {"id": "b1", "name": "Accounting", "room_type": "bureau", "parent_id": "ws1"},
]
LEVEL_ANSWERS = {
    "http://chat/rooms": ROOMS,
    "http://chat/rooms/p1/members": [{"uid": "alice", "ptype": "user", "display_name": "Alice"}, {"uid": "writerAgent", "ptype": "agent"},
                                     {"uid": "projectSecretary", "ptype": "agent"}],
    "http://chat/rooms/p2/members": [{"uid": "bob", "ptype": "user"}],
    "http://registry/agents": LEVEL_REGISTRY,
}


def level_secretary(monkeypatch, answers: dict | None = None) -> FakeHttp:
    http = FakeHttp({**LEVEL_ANSWERS, **(answers or {})})
    config = ch.ChannelConfig("http://bus", "projectSecretary", "secretary", [], "", "http://registry", chat_url="http://chat",
                              chat_key="k", accepts=["task.level_request", "task.project_record_note"], level="project")
    monkeypatch.setattr(ch, "_config", config)
    monkeypatch.setattr(ch, "_http", http)
    monkeypatch.setattr(ch, "_pending", {})
    monkeypatch.setattr(ch, "_answered", set())
    monkeypatch.setattr(ch, "_level", {})
    monkeypatch.setattr(ch, "_subtasks", {})
    monkeypatch.setattr(ch, "_subtask_results", {})
    monkeypatch.setattr(ch, "status", ch.ChannelStatus())
    return http


def test_the_level_secretary_claims_a_request_its_microagent_finds_its_own(monkeypatch):
    http = level_secretary(monkeypatch)
    seen = {}

    async def decide(level, text, origin, rooms):
        seen.update(level=level, text=text, origin=origin, rooms=rooms)
        return {"mine": True, "room_id": "p1", "why": "asked in the project's own room about this project"}

    monkeypatch.setattr(ch, "_decide_ownership", decide)
    injected = []
    run(ch._consider_level_request(level_request(), ch._config, lambda key, text, request, relay=False: injected.append((key, text, request)),
                                   None, False))
    # the microagent saw the request, where it was asked and the project rooms only, with their members
    assert seen["level"] == "project" and seen["text"].startswith("Set the objective")
    assert seen["origin"]["name"] == "Thermal Research" and seen["origin"]["parent_name"] == "Research Lab"
    assert [r["id"] for r in seen["rooms"]] == ["p1", "p2"]
    assert [m["uid"] for m in seen["rooms"][0]["members"]] == ["alice", "writerAgent"]  # itself left out
    assert seen["rooms"][0]["members"][1]["accepts"] == ["task.write_section"]
    claim = http.posted[0][1]
    assert claim["payload"]["type"] == ch.LEVEL_CLAIMED and claim["envelope"]["routing"]["targetAgentUids"] == ["chatSecretary"]
    (key, text, request), = injected
    assert key == "L1" and ch._level["L1"]["room"]["id"] == "p1"
    assert 'the project "Thermal Research"' in text and "Set the objective of this project to shipping v2" in text
    assert "project_set_objective" in text and "result.level_done" in text
    assert request == "Set the objective of this project to shipping v2"


def test_the_level_secretary_stays_silent_on_a_request_that_is_not_its_own(monkeypatch):
    http = level_secretary(monkeypatch)

    async def decide(*_a):
        return {"mine": False, "room_id": "", "why": "about the accounting bureau"}

    monkeypatch.setattr(ch, "_decide_ownership", decide)
    injected = []
    run(ch._consider_level_request(level_request(), ch._config, lambda *a, **k: injected.append(a), None, False))
    assert http.posted == [] and injected == [] and ch._pending == {}


def test_an_undecided_microagent_claims_nothing(monkeypatch):
    http = level_secretary(monkeypatch)

    async def decide(*_a):
        return None

    monkeypatch.setattr(ch, "_decide_ownership", decide)
    injected = []
    run(ch._consider_level_request(level_request(), ch._config, lambda *a, **k: injected.append(a), None, False))
    assert http.posted == [] and injected == []


def test_the_level_secretary_answers_the_sender_by_name(monkeypatch):
    http = level_secretary(monkeypatch)
    ch._pending["L1"] = level_request()
    run(ch.publish_result("L1", "result.level_done", {"summary": "The objective is now: shipping v2."}))
    body = http.posted[0][1]
    assert body["envelope"]["routing"]["targetAgentUids"] == ["chatSecretary"] and body["envelope"]["routing"]["route"] == []
    assert body["payload"] == {"type": "result.level_done", "room_id": "p1", "summary": "The objective is now: shipping v2."}
    ch._pending["L2"] = level_request("L2")
    run(ch.publish_blocked("L2", "Document Service is down"))
    blocked = http.posted[1][1]
    assert blocked["payload"] == {"type": "result.level_blocked", "room_id": "p1", "reason": "Document Service is down"}
    assert blocked["envelope"]["routing"]["targetAgentUids"] == ["chatSecretary"]


def test_subtasks_go_to_members_and_their_results_are_collected(monkeypatch):
    http = level_secretary(monkeypatch)
    ch._pending["L1"] = level_request()
    ch._level["L1"] = {"room": {"id": "p1", "name": "Thermal Research"}, "origin": {"id": "p1"}, "why": ""}
    with pytest.raises(ValueError, match='"alice" is not an agent of this room'):
        run(ch.level_publish_subtask("L1", "alice", "task.write_section", {}))
    with pytest.raises(ValueError, match="does not take task.edit_section"):
        run(ch.level_publish_subtask("L1", "writerAgent", "task.edit_section", {}))
    run(ch.level_publish_subtask("L1", "writerAgent", "task.write_section", {"doc_id": "d1", "section_name": "Plan"}))
    body = http.posted[0][1]
    assert body["envelope"]["routing"]["route"] == ["writerAgent"] and body["envelope"]["routing"]["returnTo"] == "projectSecretary"
    assert body["payload"]["room_id"] == "p1" and body["payload"]["user_uid"] == "alice"
    result = ch.make_message("writerAgent", "result.section_draft", chain_id="L1", doc_id="d1", content="...")
    assert ch._level_intake(result, "result.section_draft", "L1", ch._config, lambda *a: None, None)
    got = run(ch.wait_for_level_results("L1", 1))
    assert got["still_working"] == [] and got["results"][0]["from"] == "writerAgent"
    run(ch.on_turn_end("L1"))
    assert "L1" not in ch._level and "L1" not in ch._subtasks


def test_level_tools_refuse_a_role_that_is_not_a_level_secretary(channel):
    with pytest.raises(ValueError, match="project or bureau secretary only"):
        run(ch.level_room("L1"))


def test_the_ownership_microagent_checks_its_answer_not_its_decision():
    from app.microagents import akinakes_level_ownership as own
    rooms = [{"id": "p1", "name": "Thermal Research", "members": [{"uid": "writerAgent", "kind": "agent", "role": "writer"}]}]
    agent_input = {"level": "project", "request": "Set the objective", "origin": {"id": "p1", "name": "Thermal Research", "type": "project"},
                   "rooms": rooms}
    assert own.verify_level_ownership(agent_input, {"mine": False, "room_id": "", "why": "not a project matter"}) is None
    assert own.verify_level_ownership(agent_input, {"mine": True, "room_id": "p1", "why": "its room"}) is None
    assert "room_id" in own.verify_level_ownership(agent_input, {"mine": True, "room_id": "p9", "why": "x"})
    assert "mine" in own.verify_level_ownership(agent_input, {"mine": "yes", "room_id": "p1", "why": "x"})
    prompt = own.prompt_level_ownership(agent_input, None)
    assert "Set the objective" in prompt and '"Thermal Research"' in prompt and "writerAgent (agent: writer)" in prompt


def test_relay_text_shows_no_internal_ids_and_does_not_repeat_the_room():
    msg = {"envelope": {"senderUid": "projectSecretary"},
           "payload": {"type": "result.level_done", "room_id": "ROOM-1", "summary": "Objective set."}}
    text = ch.render_relay_text(msg, "CHAIN-9-relay-ab")
    assert "CHAIN-9" not in text and "ROOM-1" not in text and "result.level_done" not in text
    assert "Objective set." in text
    assert "chat_history first" in text and "post nothing" in text


def test_the_requester_words_say_what_the_sender_expects(monkeypatch):
    spec = {"task.make_thing": {"description": "Make a thing", "replies": [{"payload_type": "result.thing", "when": "the thing exists and has a file"}]}}
    monkeypatch.setattr(ch, "load_reply_specs", lambda: spec)
    words = ch.requester_words(task("c1", "task.make_thing"))
    assert "Make a thing. the thing exists and has a file." in words and "waits for the answer" in words
    assert "What the task is" not in ch.requester_words(task("c1", "task.unknown_thing"))


def test_the_memory_queries_are_answered_with_the_old_agents_result_types():
    recall = ch.reply_spec("query.memory_recall")
    assert [r["payload_type"] for r in recall["replies"]] == ["result.memory_recall"]
    assert recall["echo"] == ["request_uid", "scope_level"] and "query" in recall["fields"]
    ch._check_schema("result.memory_recall", recall["replies"][0]["schema"], {"results": [], "sufficient": False})
    with pytest.raises(ValueError):
        ch._check_schema("result.memory_recall", recall["replies"][0]["schema"], {"results": ["bare text"], "sufficient": True})
    remember = ch.reply_spec("query.memory_remember")
    assert [r["payload_type"] for r in remember["replies"]] == ["result.memory_remember"] and remember["echo"] == ["request_uid"]

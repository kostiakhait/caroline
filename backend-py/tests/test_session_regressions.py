"""Isolated behavioral regressions for ChatSession's SDK state machine.

The production module has native/SDK dependencies unavailable in this lightweight
test environment. These tests compile its real methods and receive loop blocks,
then replace only SDK, network, disk, and clock boundaries with inert doubles.
"""

import ast
import asyncio
import re
import time
import types
import unittest
from pathlib import Path
from typing import Any


SOURCE = Path(__file__).resolve().parents[1] / "app" / "chat_session.py"
MAIN_SOURCE = SOURCE.with_name("main.py")
TREE = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
CLASS = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "ChatSession")
METHODS = {
    node.name: node for node in CLASS.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}


def compile_methods(names, namespace):
    selected = [METHODS[name] for name in names]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)


def compile_receive_block(predicate, namespace, name):
    loop = METHODS["_run_loop"]
    receive = next(node for node in ast.walk(loop) if isinstance(node, ast.AsyncFor))
    start = next(i for i, node in enumerate(receive.body) if predicate(node))
    if name == "process_result":
        end = next(i for i, node in enumerate(receive.body[start + 1:], start + 1)
                   if isinstance(node, ast.If) and "init_received" in ast.unparse(node))
        body = receive.body[start:end]
        wrapper = ast.AsyncFunctionDef(
            name=name,
            args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self"), ast.arg(arg="message")],
                               kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=body, decorator_list=[],
        )
    else:
        cap_branch = receive.body[start]
        wrapper = ast.FunctionDef(
            name=name,
            args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")], kwonlyargs=[],
                               kw_defaults=[], defaults=[]),
            body=[ast.For(target=ast.Name(id="_", ctx=ast.Store()),
                          iter=ast.List(elts=[ast.Constant(1)], ctx=ast.Load()),
                          body=[cap_branch], orelse=[])], decorator_list=[],
        )
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 str(SOURCE), "exec"), namespace)


class AssistantMessage:
    def __init__(self, content=None):
        self.content = content or []


class TextBlock:
    def __init__(self, text):
        self.text = text


class ResultMessage:
    subtype = "success"


class ErrorResultMessage(ResultMessage):
    subtype = "error_during_execution"
    is_error = True


class SessionRegressionTests(unittest.TestCase):
    def namespace(self):
        return dict(
            Any=Any, asyncio=asyncio, time=time, log_event=lambda *args, **kwargs: None,
            RESTART_WINDOW_MS=600000, MAX_RESTARTS_PER_WINDOW=5, RESTART_BACKOFF_MS=60000,
            FORCED_COMPACTION_MIN_INTERVAL_MS=300000, FORCED_COMPACTION_HOURLY_MS=3600000,
            FORCED_COMPACTION_GROWTH_BYTES_THRESHOLD=100000,
            ONE_SHOT_FOLLOWUP_CHECK_DELAY_MS=90000,
            CONTINUE_OR_SILENT_NUDGE_TEMPLATE="Check unfinished work. Reply in {language}.",
            current_language_name=lambda tab: "English", clear_pending_turn=lambda *args: None,
            ResultMessage=ResultMessage, AssistantMessage=AssistantMessage,
            TextBlock=TextBlock,
            message_to_wire=lambda msg: {"type": "result" if isinstance(msg, ResultMessage) else "assistant"},
            _strip_no_update_from_wire=lambda wire: wire,
        )

    def test_disconnect_during_compaction_does_not_hide_replayed_user_answer(self):
        ns = self.namespace()
        compile_methods(["_check_forced_compaction", "_handle_failure", "_push_internal_command",
                         "_abandon_forced_compaction", "_complete_startup_compaction"], ns)
        compile_receive_block(
            lambda node: isinstance(node, ast.If) and any(
                isinstance(child, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "result_is_fake"
                    for target in child.targets)
                for child in ast.walk(node)), ns, "process_result")

        async def run():
            published, queued = [], []
            session = types.SimpleNamespace(
                ended=False, turn_pending=False, forced_compaction_result_pending=False,
                conn_state={"kind": "connected"}, last_saved_session_id="synthetic",
                last_forced_compaction_at=None, size_at_last_forced_compaction=None,
                _forced_compaction_retry_not_before=None,
                needs_startup_compaction=True, tab_id="test",
                workspace_dir="unused", hang_count=0, restart_timestamps=[],
                hang_interrupted_tool_name=None, hang_interrupted_tool_elapsed_s=None,
                pending_user_text=None, pending_attachments=[], hang_interrupt_result_pending=False,
                ignore_next_result_recovery=False, turn_is_voice=False,
                real_user_turn_answered=False, pending_is_real_user=True,
                _current_session_file_size=lambda: 100, queue=queued,
                _queue_event=asyncio.Event(),
                _real_turn_generation=1,
                _one_shot_followup_waiting_for_result=False,
                _one_shot_ignore_cap_until_result=False,
                on_startup_compaction_finished=lambda tab_id: None,
                _clear_mcp_reconnect_timers=lambda: None, _clear_api_retry_timer=lambda: None,
                _fire_post_turn_completion_check=lambda: None,
                _push_message=lambda *args, **kwargs: queued.append(args),
            )
            session._push_internal_command = types.MethodType(ns["_push_internal_command"], session)
            session._abandon_forced_compaction = types.MethodType(ns["_abandon_forced_compaction"], session)
            session._complete_startup_compaction = types.MethodType(ns["_complete_startup_compaction"], session)
            session._set_conn_state = lambda kind, *args: setattr(session, "conn_state", {"kind": kind})

            async def send(message):
                published.append(message)

            session.send = send
            ns["_check_forced_compaction"](session)
            self.assertTrue(session.forced_compaction_result_pending)
            self.assertEqual(queued[0]["message"]["message"]["content"][0]["text"], "/compact")
            queued.append({"message": {"type": "user", "message": {"content": [{"text": "REAL USER TASK"}]}}, "is_voice": False})
            session.pending_user_text = "REAL USER TASK"
            session.turn_pending = True
            await ns["_handle_failure"](session, RuntimeError("transport disconnected during compact"))
            self.assertTrue(session.needs_startup_compaction)
            self.assertIsNone(session.last_forced_compaction_at)
            self.assertNotIn("/compact", [item["message"]["message"]["content"][0]["text"]
                                         for item in session.queue if isinstance(item, dict)])
            self.assertIn("REAL USER TASK", [item["message"]["message"]["content"][0]["text"]
                                             for item in session.queue if isinstance(item, dict)])
            session.conn_state = {"kind": "connected"}
            await ns["process_result"](session, AssistantMessage())
            await ns["process_result"](session, ResultMessage())
            self.assertEqual([item["message"]["type"] for item in published], ["assistant", "result"])
            self.assertFalse(session.turn_pending)
            self.assertIsNone(session.pending_user_text)
            ns["_check_forced_compaction"](session)
            self.assertTrue(session.forced_compaction_result_pending)

        asyncio.run(run())

    def test_startup_compaction_result_handles_success_and_error(self):
        ns = self.namespace()
        compile_methods(["_check_forced_compaction", "_complete_startup_compaction",
                         "_abandon_forced_compaction"], ns)
        compile_receive_block(
            lambda node: isinstance(node, ast.If) and any(
                isinstance(child, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "result_is_fake"
                    for target in child.targets)
                for child in ast.walk(node)), ns, "process_result")
        published, completed = [], []
        session = types.SimpleNamespace(
            ended=False, turn_pending=False, needs_startup_compaction=True,
            forced_compaction_result_pending=False, forced_compaction_reason=None,
            conn_state={"kind": "connected"}, last_saved_session_id="synthetic",
            last_forced_compaction_at=None, size_at_last_forced_compaction=None,
            _forced_compaction_retry_not_before=None,
            tab_id="test", workspace_dir="unused", hang_interrupt_result_pending=False,
            ignore_next_result_recovery=False, pending_is_real_user=False,
            turn_is_voice=False, real_user_turn_answered=False,
            _one_shot_followup_waiting_for_result=False,
            _one_shot_ignore_cap_until_result=False,
            queue=[],
            _current_session_file_size=lambda: 100,
            _push_internal_command=lambda text: None,
            _clear_api_retry_timer=lambda: None,
            on_startup_compaction_finished=lambda tab_id: completed.append(tab_id),
            on_startup_compaction_retry_scheduled=lambda tab_id, deadline: None,
        )
        session._complete_startup_compaction = types.MethodType(ns["_complete_startup_compaction"], session)
        session._abandon_forced_compaction = types.MethodType(ns["_abandon_forced_compaction"], session)
        session._set_conn_state = lambda kind, *args: setattr(session, "conn_state", {"kind": kind})

        async def send(message):
            published.append(message)

        session.send = send

        async def run():
            ns["_check_forced_compaction"](session)
            await ns["process_result"](session, AssistantMessage())
            await ns["process_result"](session, ResultMessage())

        asyncio.run(run())
        self.assertEqual(published, [])
        self.assertEqual(completed, ["test"])
        self.assertFalse(session.forced_compaction_result_pending)
        self.assertFalse(session.needs_startup_compaction)
        self.assertIsNotNone(session.last_forced_compaction_at)

        session.needs_startup_compaction = True
        session.last_forced_compaction_at = None

        async def fail():
            ns["_check_forced_compaction"](session)
            await ns["process_result"](session, ErrorResultMessage())

        asyncio.run(fail())
        self.assertEqual(completed, ["test"])
        self.assertTrue(session.needs_startup_compaction)
        self.assertFalse(session.forced_compaction_result_pending)
        self.assertIsNone(session.last_forced_compaction_at)
        ns["_check_forced_compaction"](session)
        self.assertFalse(session.forced_compaction_result_pending)
        self.assertTrue(session.needs_startup_compaction)
        # Error results back off for the normal five-minute maintenance
        # interval; a transport disconnect (covered above) still retries
        # without this error-result delay.
        now = time.monotonic()
        ns["time"] = types.SimpleNamespace(monotonic=lambda: now + 301)
        ns["_check_forced_compaction"](session)
        self.assertTrue(session.forced_compaction_result_pending)

    def test_usage_cap_check_does_not_schedule_itself_again(self):
        ns = self.namespace()
        compile_methods(["_schedule_one_shot_followup_check", "_clear_one_shot_followup_timer",
                         "submit_or_try_small_model"], ns)
        compile_receive_block(
            lambda node: isinstance(node, ast.If) and "cc_cli_limit_message" in ast.unparse(node),
            ns, "receive_cap")
        compile_receive_block(
            lambda node: isinstance(node, ast.If) and any(
                isinstance(child, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "result_is_fake"
                    for target in child.targets)
                for child in ast.walk(node)), ns, "process_result")
        callbacks, delivered = [], []

        class FakeHandle:
            def __init__(self, fn):
                self.fn = fn
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        class FakeLoop:
            def call_later(self, delay, fn):
                handle = FakeHandle(fn)
                callbacks.append(handle)
                return handle

        ns.update(asyncio=types.SimpleNamespace(get_event_loop=lambda: FakeLoop()),
                  message=AssistantMessage([TextBlock("You've hit your session limit")]),
                  CC_CLI_LIMIT_PATTERN=re.compile("session limit"), SMALL_MODEL_ENABLED=False)

        def make_session():
            session = types.SimpleNamespace(
                one_shot_followup_timer=None, ended=False, tab_id="test", turn_is_voice=False,
                _real_turn_generation=1, _one_shot_consumed_generation=-1,
                _one_shot_followup_waiting_for_result=False,
                _one_shot_ignore_cap_until_result=False,
                small_model_active=False, turn_pending=True, pending_is_real_user=False,
                pending_user_text=None, pending_attachments=[],
                hang_interrupt_result_pending=False, ignore_next_result_recovery=False,
                forced_compaction_result_pending=False, classifier_refusal_retry_count=0,
                forced_compaction_reason=None, _forced_compaction_previous_clock=None,
                last_api_retry_error=None, consecutive_auth_retry_failures=0,
                conn_state={"kind": "connected"}, workspace_dir="unused",
                real_user_turn_answered=False,
                _set_conn_state=lambda *args, **kwargs: None,
                _clear_api_retry_timer=lambda: None,
                _fire_post_turn_completion_check=lambda: None,
            )
            session._schedule_one_shot_followup_check = types.MethodType(
                ns["_schedule_one_shot_followup_check"], session)
            session._clear_one_shot_followup_timer = types.MethodType(
                ns["_clear_one_shot_followup_timer"], session)
            session.submit_or_try_small_model = types.MethodType(
                ns["submit_or_try_small_model"], session)
            session.submit = lambda *args, **kwargs: setattr(session, "turn_pending", True)
            session.inject_proactive = lambda *args, **kwargs: delivered.append(args)

            async def send(message):
                pass

            session.send = send
            return session

        async def finish(session):
            await ns["process_result"](session, ResultMessage())

        async def run():
            session = make_session()
            ns["receive_cap"](session)
            self.assertEqual(len(callbacks), 1)
            await finish(session)
            callbacks[0].fn()
            self.assertEqual(len(delivered), 1)
            self.assertTrue(session._one_shot_followup_waiting_for_result)
            ns["receive_cap"](session)
            self.assertEqual(len(callbacks), 1)
            await finish(session)
            self.assertFalse(session._one_shot_followup_waiting_for_result)

            # A later distinct real turn gets exactly one new allowance.
            session.submit_or_try_small_model("new user task")
            ns["receive_cap"](session)
            self.assertEqual(len(callbacks), 2)
            await finish(session)
            callbacks[1].fn()
            session.submit_or_try_small_model("third task while old check is in flight")
            ns["receive_cap"](session)
            self.assertEqual(len(callbacks), 2)
            await finish(session)
            ns["receive_cap"](session)
            self.assertEqual(len(callbacks), 3)

            # The older real turn's FIRST cap may arrive after the newer
            # submit. Ignore that cap through its terminal result so it
            # cannot consume the new turn's allowance.
            late = make_session()
            late.submit_or_try_small_model("newer task")
            ns["receive_cap"](late)
            self.assertIsNone(late.one_shot_followup_timer)
            await finish(late)
            ns["receive_cap"](late)
            self.assertIsNotNone(late.one_shot_followup_timer)

            # A maintenance command's fake result is not the terminal
            # result of an in-flight usage-cap check.
            compact = make_session()
            compact._one_shot_followup_waiting_for_result = True
            compact.forced_compaction_result_pending = True
            await finish(compact)
            self.assertTrue(compact._one_shot_followup_waiting_for_result)

        asyncio.run(run())

    def test_startup_compaction_intent_survives_websocket_reconnect(self):
        main_tree = ast.parse(MAIN_SOURCE.read_text(encoding="utf-8-sig"))
        endpoint = next(node for node in main_tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "ws_endpoint")
        begin = next(i for i, node in enumerate(endpoint.body) if isinstance(node, ast.FunctionDef)
                     and node.name == "mark_startup_compaction_finished")
        end = next(i for i, node in enumerate(endpoint.body[begin:], begin)
                   if isinstance(node, ast.Expr) and isinstance(node.value, ast.Await)
                   and isinstance(node.value.value, ast.Call)
                   and isinstance(node.value.value.func, ast.Attribute)
                   and node.value.value.func.attr == "start")
        wrapper = ast.AsyncFunctionDef(
            name="register",
            args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="websocket"), ast.arg(arg="tab_id")],
                               kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=endpoint.body[begin:end + 1] + [ast.Return(value=ast.Name(id="session", ctx=ast.Load()))],
            decorator_list=[],
        )
        sessions, completed, retry_after = {}, set(), {}

        class FakeSession:
            def __init__(self, tab_id, workspace_dir, send, on_startup_compaction_finished, **kwargs):
                self.tab_id = tab_id
                self.on_startup_compaction_finished = on_startup_compaction_finished
                self.on_startup_compaction_retry_scheduled = kwargs.get(
                    "on_startup_compaction_retry_scheduled", lambda *args: None)
                self.retry_not_before = kwargs.get("startup_compaction_retry_not_before")
                self.needs_startup_compaction = False

            async def start(self):
                self.registered_with_intent = sessions[self.tab_id] is self and self.needs_startup_compaction

        ns = dict(ChatSession=FakeSession, sessions=sessions,
                  _completed_startup_compaction_for_tab=completed,
                  _startup_compaction_retry_not_before_for_tab=retry_after,
                  WORKSPACE_DIR="unused")
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                     str(MAIN_SOURCE), "exec"), ns)
        websocket = types.SimpleNamespace(send_json=lambda value: None)

        async def run():
            first = await ns["register"](websocket, "2")
            self.assertTrue(first.registered_with_intent)
            retry_deadline = time.monotonic() + 300
            first.on_startup_compaction_retry_scheduled("2", retry_deadline)
            second = await ns["register"](websocket, "2")
            self.assertTrue(second.registered_with_intent)
            self.assertEqual(second.retry_not_before, retry_deadline)
            first.on_startup_compaction_retry_scheduled("2", retry_deadline + 300)
            self.assertEqual(retry_after["2"], retry_deadline)
            first.on_startup_compaction_finished("2")
            self.assertNotIn("2", completed)  # stale connection cannot finish it
            second.on_startup_compaction_finished("2")
            self.assertIn("2", completed)
            third = await ns["register"](websocket, "2")
            self.assertFalse(third.needs_startup_compaction)
            self.assertIsNone(third.retry_not_before)

        asyncio.run(run())

    def test_startup_intent_is_visible_before_compaction_and_clears_without_history(self):
        ns = self.namespace()
        compile_methods(["status", "_complete_startup_compaction"], ns)
        completed = []
        session = types.SimpleNamespace(
            tab_id="2", ended=False, turn_pending=False, has_seen_init=False,
            last_activity=time.monotonic(), last_user_activity=time.monotonic(),
            hang_count=0, conn_state={"kind": "connected"},
            needs_startup_compaction=True, forced_compaction_result_pending=False,
            on_startup_compaction_finished=lambda tab_id: completed.append(tab_id),
        )
        self.assertTrue(ns["status"](session)["forcedCompactionPending"])
        ns["_complete_startup_compaction"](session)
        self.assertFalse(ns["status"](session)["forcedCompactionPending"])
        self.assertEqual(completed, ["2"])

if __name__ == "__main__":
    unittest.main()

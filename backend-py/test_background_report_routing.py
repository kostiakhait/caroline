"""A finished background shell command goes to its own pseudo-session only when the session that started it is idle;
mid-turn, the CLI hands the notification to that turn itself. Run: python -m pytest test_background_report_routing.py -q"""
import asyncio
from types import SimpleNamespace

from claude_agent_sdk import TaskNotificationMessage

from app.chat_session_mixins import watchdog as wd


def _note(task_id="b1"):
    return TaskNotificationMessage(subtype="task_notification", data={}, task_id=task_id, status="completed",
                                   output_file="x", summary="done", uuid="u", session_id="s")


def _run(monkeypatch, task_type, turn_pending):
    created = []
    monkeypatch.setattr(wd.agent_registry, "running", lambda ws, tab: {"b1": {"description": "cp a b", "task_type": task_type}})
    monkeypatch.setattr(wd.agent_registry, "finish", lambda *a, **k: None)

    class Pseudo:
        tab_id = "1-bg-b1"

        def inject_proactive(self, text, suppress_narration=False):
            created.append(text)

    async def get_or_create(real_tab, activity, getter):
        return Pseudo()

    monkeypatch.setattr(wd.pseudo_tab, "get_or_create_pseudo_session", get_or_create)
    monkeypatch.setattr(wd.pseudo_tab, "set_pseudo_turn_task", lambda *a: None)
    monkeypatch.setattr(wd, "get_sessions_getter", lambda: (lambda: {}))
    import app.background_context as bc
    monkeypatch.setattr(bc, "context_block", lambda *a: "")
    stub = SimpleNamespace(workspace_dir="ws", tab_id="1", turn_pending=turn_pending,
                           inject_proactive=lambda *a, **k: created.append("self"))
    asyncio.run(wd.WatchdogMixin._track_background_tasks(stub, _note()))
    return created


def test_shell_command_mid_turn_stays_with_the_live_turn(monkeypatch):
    assert _run(monkeypatch, "local_bash", turn_pending=True) == []


def test_shell_command_after_the_turn_gets_its_own_thread(monkeypatch):
    assert len(_run(monkeypatch, "local_bash", turn_pending=False)) == 1


def test_real_sub_agent_mid_turn_still_gets_its_own_thread(monkeypatch):
    assert len(_run(monkeypatch, "local_agent", turn_pending=True)) == 1

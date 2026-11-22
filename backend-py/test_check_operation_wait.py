"""check_operation_status waits for a running operation (2026-10-29): every "still running"
answer used to cost a whole model call, so the model polled it over and over (169 polls of
the mail report in one day). Run: python -m pytest test_check_operation_wait.py -q"""

import asyncio

from app import operations


def _start(seconds: float) -> "operations.Operation":
    op = operations.REGISTRY.create("slow_tool", tab_id=None)

    async def run():
        await asyncio.sleep(seconds)
        op.status = "done"
        op.result = {"text": "ok"}
        return op.result

    op.task = asyncio.get_running_loop().create_task(run())
    return op


def _check(op_id: str) -> str:
    return operations.check_operation_status.handler({"operation_id": op_id})


def test_a_status_check_returns_the_result_once_the_operation_finishes(monkeypatch):
    monkeypatch.setattr(operations, "CHECK_OPERATION_WAIT_S", 5.0)

    async def scenario():
        op = _start(0.2)
        started = asyncio.get_running_loop().time()
        reply = await _check(op.id)
        return reply, asyncio.get_running_loop().time() - started

    reply, took = asyncio.run(scenario())
    text = reply["content"][0]["text"]
    assert "'status': 'done'" in text and "'ok'" in text
    assert took < 2.0


def test_a_status_check_answers_running_when_the_wait_is_up(monkeypatch):
    monkeypatch.setattr(operations, "CHECK_OPERATION_WAIT_S", 0.1)

    async def scenario():
        op = _start(5.0)
        reply = await _check(op.id)
        op.task.cancel()
        return reply

    assert "'status': 'running'" in asyncio.run(scenario())["content"][0]["text"]


def test_a_finished_operation_is_answered_at_once(monkeypatch):
    monkeypatch.setattr(operations, "CHECK_OPERATION_WAIT_S", 60.0)

    async def scenario():
        op = _start(0)
        await asyncio.sleep(0.05)
        started = asyncio.get_running_loop().time()
        reply = await _check(op.id)
        return reply, asyncio.get_running_loop().time() - started

    reply, took = asyncio.run(scenario())
    assert "'status': 'done'" in reply["content"][0]["text"] and took < 0.5

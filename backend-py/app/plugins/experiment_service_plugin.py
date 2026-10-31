"""Read access to akinakes' Experiment Service (EXPERIMENT_SERVICE_URL), for the planner and the analyst: experiment
plans and runs, the way the old analyst read them (agents/analyst/analyst_agent.py). Not loaded when
EXPERIMENT_SERVICE_URL is not set."""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.plugins.document_service_plugin import TIMEOUT_S, _failure, _reply, _tool
from app.plugins.loader import Plugin


def _base() -> str:
    return (os.environ.get("EXPERIMENT_SERVICE_URL") or "").strip().rstrip("/")


async def _get(path: str) -> Any:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(f"{_base()}/experiments{path}")
    if resp.status_code == 404:
        raise RuntimeError(f"Not found: {path}")
    resp.raise_for_status()
    return resp.json()


async def experiment_get_run(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await _get(f"/runs/{args['run_id']}"))
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)


async def experiment_get_plan(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await _get(f"/plans/{args['plan_id']}"))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def experiment_list_plans(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await _get("/plans"))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


# Only where the Experiment Service exists (EXPERIMENT_SERVICE_URL set): elsewhere the plugin is not loaded at all.
PLUGIN = None if not _base() else Plugin(
    name="experiment_service",
    description="Read experiment plans and runs of the organization's Experiment Service",
    tools=[
        _tool("experiment_get_run", "One experiment run: its plan_id, status and result.",
              {"run_id": {"type": "string"}}, ["run_id"], experiment_get_run),
        _tool("experiment_get_plan", "One experiment plan: its objective, hypothesis, methodology and status.",
              {"plan_id": {"type": "string"}}, ["plan_id"], experiment_get_plan),
        _tool("experiment_list_plans", "All experiment plans.", {}, [], experiment_list_plans),
    ],
)

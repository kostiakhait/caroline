"""Project and bureau state in akinakes' Document Service, for the two secretaries: each tool is one of the old agents'
operations, done exactly as they did it (agents/secretary/{secretary_base,project_secretary,bureau_secretary}.py), so
the model chooses the operation and its words but never rebuilds the stored JSON itself (owner's decision, 2026-10-31).

- project state: one "project-state" document per project_id (tag project-state), its first section holds the JSON
  {objectives, decisions, tasks, status};
- bureau decisions: one "bureau-decisions" document per bureau_id (tags bureau-decisions,<bureau_id>), a section per
  decision; a bureau summary is a "bureau-summary" document of its own.

Not loaded when DOCUMENT_SERVICE_URL is not set."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from app.plugins.document_service_plugin import TIMEOUT_S, _base, _failure, _reply, _tool
from app.plugins.loader import Plugin

AGENT_UID = (os.environ.get("AGENT_UID") or "").strip() or "secretary"
EMPTY_STATE = {"objectives": [], "decisions": [], "tasks": [], "status": "active"}


async def _call(method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.request(method, f"{_base()}{path}", params=params, json=body)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


async def _with_sections(doc: dict[str, Any]) -> dict[str, Any]:
    if "sections" in doc or not doc.get("id"):
        return doc
    return await _call("GET", f"/documents/{doc['id']}")


async def _project_doc(project_id: str) -> dict[str, Any]:
    docs = await _call("GET", "/documents", params={"project_id": project_id, "tags": "project-state"})
    if docs:
        return await _with_sections(docs[0])
    return await _call("POST", "/documents", body={
        "type": "project-state", "title": f"Project State — {project_id}", "format": "json", "project_id": project_id,
        "visibility": "project", "created_by": AGENT_UID, "tags": ["project-state"],
    })


def _state(doc: dict[str, Any]) -> dict[str, Any]:
    sections = doc.get("sections") or []
    if not sections:
        return json.loads(json.dumps(EMPTY_STATE))
    try:
        return json.loads(sections[0].get("content") or "{}")
    except (ValueError, TypeError):
        return {}


async def _save_state(doc: dict[str, Any], state: dict[str, Any]) -> None:
    content = json.dumps(state, ensure_ascii=False, indent=2)
    sections = doc.get("sections") or []
    if sections:
        await _call("PATCH", f"/documents/{doc['id']}/sections/{sections[0]['id']}", body={"content": content, "agent_uid": AGENT_UID})
    else:
        await _call("POST", f"/documents/{doc['id']}/sections",
                    body={"title": "state", "content": content, "content_type": "json_node", "order": 1})


async def _append(project_id: str, key: str, item: dict[str, Any]) -> dict[str, Any]:
    if not project_id:
        raise RuntimeError("project_id required")
    doc = await _project_doc(project_id)
    if not doc.get("id"):
        raise RuntimeError("Could not create/find project document")
    state = _state(doc)
    state.setdefault(key, []).append(item)
    await _save_state(doc, state)
    return {"project_id": project_id, "doc_id": doc["id"], key: state[key]}


async def project_state_get(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        doc = await _project_doc(str(args.get("project_id") or ""))
        return _reply({"project_id": args.get("project_id"), "doc_id": doc.get("id"), "state": _state(doc)})
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)


async def project_add_objective(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await _append(str(args.get("project_id") or ""), "objectives",
                                    {"text": args.get("objective") or "", "set_by": args.get("set_by") or "user"}))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def project_add_decision(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await _append(str(args.get("project_id") or ""), "decisions", {
            "text": args.get("decision") or "", "decided_by": args.get("decided_by") or "user", "chain_id": args.get("chain_id"),
        }))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def project_assign_task(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await _append(str(args.get("project_id") or ""), "tasks", {
            "title": args.get("task_title") or "", "assignee": args.get("assignee") or "", "status": "assigned",
            "chain_id": args.get("chain_id"),
        }))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def _decisions_doc(bureau_id: str) -> dict[str, Any]:
    docs = await _call("GET", "/documents", params={"tags": f"bureau-decisions,{bureau_id}"})
    if docs:
        return await _with_sections(docs[0])
    return await _call("POST", "/documents", body={
        "type": "bureau-decisions", "title": f"Decisions — {bureau_id}", "format": "markdown", "visibility": "internal",
        "created_by": AGENT_UID, "tags": ["bureau-decisions", bureau_id],
    })


async def bureau_capture_decision(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    bureau_id, decision, context = str(args.get("bureau_id") or ""), str(args.get("decision") or ""), str(args.get("context") or "")
    try:
        if not bureau_id or not decision:
            raise RuntimeError("bureau_id and decision required")
        doc = await _decisions_doc(bureau_id)
        if not doc.get("id"):
            raise RuntimeError("Could not create decisions document")
        content = f"**{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}** — {decision}"
        if context:
            content += f"\n\n*Context:* {context}"
        await _call("POST", f"/documents/{doc['id']}/sections", body={
            "title": decision[:80], "content": content, "content_type": "text", "section_type": "параграф",
            "order": len(doc.get("sections") or []) + 1,
        })
        return _reply({"bureau_id": bureau_id, "doc_id": doc["id"]})
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def bureau_add_summary(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    bureau_id, summary = str(args.get("bureau_id") or ""), str(args.get("summary") or "")
    title = str(args.get("title") or f"Summary {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    try:
        if not bureau_id or not summary:
            raise RuntimeError("bureau_id and summary required")
        doc = await _call("POST", "/documents", body={
            "type": "bureau-summary", "title": title, "format": "markdown", "visibility": "internal",
            "created_by": AGENT_UID, "tags": ["bureau-summary", bureau_id],
        })
        if not doc.get("id"):
            raise RuntimeError("Could not create summary document")
        await _call("POST", f"/documents/{doc['id']}/sections",
                    body={"title": "Summary", "content": summary, "content_type": "text", "order": 1})
        return _reply({"bureau_id": bureau_id, "doc_id": doc["id"]})
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def bureau_list_decisions(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    bureau_id = str(args.get("bureau_id") or "")
    try:
        if not bureau_id:
            raise RuntimeError("bureau_id required")
        doc = await _decisions_doc(bureau_id)
        return _reply({"bureau_id": bureau_id, "doc_id": doc.get("id"),
                       "decisions": [{"title": s.get("title"), "content": s.get("content")} for s in doc.get("sections") or []]})
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


_STR = {"type": "string"}
_PROJECT = {"project_id": _STR}

# Only where the Document Service exists (DOCUMENT_SERVICE_URL set): elsewhere the plugin is not loaded at all.
PLUGIN = None if not _base() else Plugin(
    name="project_state",
    description="Project state (objectives, decisions, tasks) and bureau decisions and summaries",
    tools=[
        _tool("project_state_get", "The project's state: objectives, decisions, tasks, status, and its document id.",
              _PROJECT, ["project_id"], project_state_get),
        _tool("project_add_objective", "Adds an objective to the project's state.",
              {**_PROJECT, "objective": _STR, "set_by": _STR}, ["project_id", "objective"], project_add_objective),
        _tool("project_add_decision", "Records a decision in the project's state.",
              {**_PROJECT, "decision": _STR, "decided_by": _STR, "chain_id": _STR}, ["project_id", "decision"], project_add_decision),
        _tool("project_assign_task", "Adds a task with its assignee to the project's state (status assigned).",
              {**_PROJECT, "task_title": _STR, "assignee": _STR, "chain_id": _STR}, ["project_id", "task_title", "assignee"],
              project_assign_task),
        _tool("bureau_capture_decision", "Records a bureau's decision, with its context, in the bureau's decisions document.",
              {"bureau_id": _STR, "decision": _STR, "context": _STR}, ["bureau_id", "decision"], bureau_capture_decision),
        _tool("bureau_add_summary", "Stores a summary of a bureau's discussion as a document of its own.",
              {"bureau_id": _STR, "summary": _STR, "title": _STR}, ["bureau_id", "summary"], bureau_add_summary),
        _tool("bureau_list_decisions", "The bureau's recorded decisions and its decisions document id.",
              {"bureau_id": _STR}, ["bureau_id"], bureau_list_decisions),
    ],
)

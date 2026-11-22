"""Writing to akinakes' Document Service (DOCUMENT_SERVICE_URL), for the Agent Bus roles that change documents: the
editor (applies an edit, or posts an edit suggestion), the planner and the analyst (create a document, add sections).
The same endpoints the old agents called (agents/{editor,planner,analyst}). A role that only proposes text (the writer)
leaves this plugin out (CAROLINE_EXCLUDED_PLUGINS). Not loaded when DOCUMENT_SERVICE_URL is not set."""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.plugins.document_service_plugin import TIMEOUT_S, _base, _failure, _reply, _tool
from app.plugins.loader import Plugin

AGENT_UID = (os.environ.get("AGENT_UID") or "").strip()


async def _send(method: str, path: str, body: dict[str, Any]) -> Any:
    base = _base()
    if not base:
        raise RuntimeError("DOCUMENT_SERVICE_URL is not set: the Document Service is not available here.")
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.request(method, f"{base}{path}", json=body)
    if resp.status_code == 404:
        raise RuntimeError(f"Not found: {path}")
    if resp.status_code == 409:
        raise RuntimeError(f"Refused: {resp.json().get('detail', 'conflict')}")
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _given(args: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: args[k] for k in keys if args.get(k) not in (None, "")}


async def document_create(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    body = {"type": args["type"], "title": args["title"], "format": args.get("format") or "markdown",
            "created_by": AGENT_UID or "agent", "tags": list(args.get("tags") or []),
            **_given(args, "project_id", "parent_id", "visibility", "priority")}
    try:
        doc = await _send("POST", "/documents", body)
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)
    return _reply({"id": doc.get("id"), "title": doc.get("title"), "type": doc.get("type")})


async def document_add_section(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    body = {"title": args["title"], "content": args.get("content") or "", "content_type": args.get("content_type") or "text",
            **_given(args, "order", "section_type", "parent_section_id")}
    try:
        section = await _send("POST", f"/documents/{args['doc_id']}/sections", body)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return _reply({"id": section.get("id"), "title": section.get("title")})


async def document_patch_section(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        await _send("PATCH", f"/documents/{args['doc_id']}/sections/{args['section_id']}",
                    {"content": args["content"], "agent_uid": AGENT_UID or None, **_given(args, "chain_id")})
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return {"text": f"Section {args['section_id']} updated."}


async def document_propose_edit(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        edit = await _send("POST", f"/documents/{args['doc_id']}/edits", {
            "section_id": args["section_id"], "content": args["content"], "type": "suggestion",
            "author_uid": AGENT_UID or "agent", **_given(args, "chain_id"),
        })
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return _reply({"edit_id": edit.get("id"), "section_id": args["section_id"]})


_DOC_ID = {"doc_id": {"type": "string"}}
_STR = {"type": "string"}

# Only where the Document Service exists (DOCUMENT_SERVICE_URL set): elsewhere the plugin is not loaded at all.
PLUGIN = None if not _base() else Plugin(
    name="document_write",
    description="Change documents of the organization's Document Service",
    tools=[
        _tool("document_create", "Creates a document (sections are added with document_add_section). Returns its id.",
              {"type": _STR, "title": _STR, "format": _STR, "project_id": _STR, "parent_id": _STR,
               "tags": {"type": "array", "items": _STR}, "visibility": _STR, "priority": _STR},
              ["type", "title"], document_create),
        _tool("document_add_section", "Adds a section to a document. Returns its id.",
              {**_DOC_ID, "title": _STR, "content": _STR, "content_type": _STR, "order": {"type": "integer"},
               "section_type": _STR, "parent_section_id": _STR}, ["doc_id", "title"], document_add_section),
        _tool("document_patch_section", "Replaces the content of a section (the edit is applied, not proposed).",
              {**_DOC_ID, "section_id": _STR, "content": _STR, "chain_id": _STR}, ["doc_id", "section_id", "content"],
              document_patch_section),
        _tool("document_propose_edit", "Posts new content for a section as an edit suggestion for people to accept or "
              "reject. Returns the edit's id.", {**_DOC_ID, "section_id": _STR, "content": _STR, "chain_id": _STR},
              ["doc_id", "section_id", "content"], document_propose_edit),
    ],
)

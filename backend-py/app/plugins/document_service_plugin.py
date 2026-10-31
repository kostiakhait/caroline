"""Read access to akinakes' Document Service (DOCUMENT_SERVICE_URL), for the Agent Bus agents: the document's own
description, plan and synopsis, its sections, documents by project or tags, and full-text search. Read only; writing
is document_write_plugin.py, left out of the roles that only propose text. Not loaded when DOCUMENT_SERVICE_URL is not
set."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.plugins.loader import Plugin, PluginTool

MAX_TEXT_CHARS = 20000
TIMEOUT_S = 15


def _base() -> str:
    return (os.environ.get("DOCUMENT_SERVICE_URL") or "").strip().rstrip("/")


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    base = _base()
    if not base:
        raise RuntimeError("DOCUMENT_SERVICE_URL is not set: the Document Service is not available here.")
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(f"{base}{path}", params=params)
    if resp.status_code == 404:
        raise RuntimeError(f"Not found: {path}")
    resp.raise_for_status()
    return resp.json()


def _reply(data: Any) -> dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n... (cut; ask for one section at a time)"
    return {"text": text}


def _failure(exc: Exception) -> dict[str, Any]:
    return {"is_error": True, "content": [{"type": "text", "text": str(exc)}]}


async def document_get(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await _get(f"/documents/{args['doc_id']}"))
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)


async def document_list_sections(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        sections = await _get(f"/documents/{args['doc_id']}/sections")
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    # The outline only: ids, titles, types and synopses. Content comes one section at a time.
    keep = ("id", "title", "section_type", "parent_section_id", "order", "synopsis")
    return _reply([{k: s[k] for k in keep if k in s} for s in sections])


async def document_get_section(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await _get(f"/documents/{args['doc_id']}/sections/{args['section_id']}"))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def document_list(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    params = {k: str(args[k]) for k in ("project_id", "tags", "parent_id") if args.get(k)}
    try:
        docs = await _get("/documents", params)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    keep = ("id", "type", "title", "project_id", "parent_id", "tags", "created_by", "synopsis")
    return _reply([{k: d[k] for k in keep if k in d} for d in docs])


async def document_search(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await _get("/documents/search", {"q": args["query"], "limit": int(args.get("limit") or 10)}))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str], handler: Any) -> PluginTool:
    return PluginTool(name=name, description=description, handler=handler,
                      input_schema={"type": "object", "properties": properties, "required": required})


_DOC_ID = {"doc_id": {"type": "string"}}

# Only where the Document Service exists (DOCUMENT_SERVICE_URL set): elsewhere the plugin is not loaded at all.
PLUGIN = None if not _base() else Plugin(
    name="document_service",
    description="Read documents of the organization's Document Service",
    tools=[
        _tool("document_get", "A document's title, description, plan and synopsis.", _DOC_ID, ["doc_id"], document_get),
        _tool("document_list_sections", "The outline of a document: its sections with ids, titles and synopses (no content).",
              _DOC_ID, ["doc_id"], document_list_sections),
        _tool("document_get_section", "One section of a document in full, with its content.",
              {**_DOC_ID, "section_id": {"type": "string"}}, ["doc_id", "section_id"], document_get_section),
        _tool("document_list", "Documents by project, tags (comma separated, all must match) or parent document: ids, types, "
              "titles, tags.", {"project_id": {"type": "string"}, "tags": {"type": "string"}, "parent_id": {"type": "string"}},
              [], document_list),
        _tool("document_search", "Full-text search across the sections of all documents.",
              {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"], document_search),
    ],
)

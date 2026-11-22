"""The akinakes document maintainer, as an event subscriber of a Caroline role (AGENT_ROLE=maintainer).

The old DocumentMaintainerAgent listened to Document Service events (a section written or created, a document imported)
and kept a short synopsis on every section, rolled up through the parent sections to the document. Here the same events
reach the role through akinakes_channel (AGENT_SUBSCRIBES) and become one turn per document: the model writes the
synopses with its tools (plugins/akinakes_maintainer_plugin.py), this module only says WHAT is out of date, by comparing
hashes of the content (no reading of text).

A burst of events for one document (a creator writing ten sections) is one turn: the first event starts a quiet period,
every further event for the document restarts it, and the turn starts when it has passed. Events that arrive during the
turn make one more turn after it. Only this role subscribes, so no other Caroline user is touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Any, Awaitable, Callable

import httpx

from app.logging_setup import log_event

TIMEOUT_S = 20
MAX_CONTENT_CHARS = 3000  # as the old agent: the model reads this much of a section

# doc_id -> time of its last event; the documents waiting for a quiet period; the documents with a turn running (-> chain)
_last: dict[str, float] = {}
_waiting: dict[str, "asyncio.Task[None]"] = {}
_running: dict[str, str] = {}
_dirty: set[str] = set()
# doc_id -> sections whose synopsis was written (or whose children's were) since their roll-up was last written
_touched: dict[str, set[str]] = {}
_starter: Callable[[str], Awaitable[str | None]] | None = None


def quiet_seconds() -> float:
    try:
        return float(os.environ.get("MAINTAINER_QUIET_S") or 20)
    except ValueError:
        return 20.0


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _base() -> str:
    return (os.environ.get("DOCUMENT_SERVICE_URL") or "").strip().rstrip("/")


async def _get(client: httpx.AsyncClient, path: str) -> Any:
    resp = await client.get(f"{_base()}{path}")
    resp.raise_for_status()
    return resp.json()


# --- what is out of date --------------------------------------------------------------------------------------------------

def analyse(doc: dict[str, Any], sections: list[dict[str, Any]], touched: set[str] | None = None) -> dict[str, Any]:
    """The work of one document. A leaf needs a synopsis when it has content and its synopsis was made from other content
    (the synopsis_hash is not the hash of the content now). A parent needs a roll-up of its children's synopses when they
    all exist and its own is missing, made from other content, or a section under it was written since. The document
    needs its synopsis when it has none or any section was written since."""
    touched = touched or set()
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for s in sections:
        by_parent.setdefault(s.get("parent_section_id") or None, []).append(s)
    ids = {s["id"] for s in sections}
    # a parent that is not in the document is treated as a top-level section
    for pid in [p for p in by_parent if p and p not in ids]:
        by_parent.setdefault(None, []).extend(by_parent.pop(pid))

    def depth(s: dict[str, Any]) -> int:
        d, seen = 0, set()
        while s.get("parent_section_id") in ids and s["id"] not in seen:
            seen.add(s["id"])
            s = next(x for x in sections if x["id"] == s["parent_section_id"])
            d += 1
        return d

    leaves, parents = [], []
    for s in sections:
        children = by_parent.get(s["id"]) or []
        content = s.get("content") or ""
        if not children:
            if content.strip() and not s.get("locked_by") and sha256(content) != s.get("synopsis_hash"):
                leaves.append({"id": s["id"], "title": s.get("title", ""), "content": content[:MAX_CONTENT_CHARS]})
            continue
        # Ready when every child that can have a synopsis has one (an empty leaf never does and is left out).
        if any(not c.get("synopsis") and ((c.get("content") or "").strip() or by_parent.get(c["id"])) for c in children):
            continue
        children = [c for c in children if c.get("synopsis")]
        if not children:
            continue
        outdated = (not s.get("synopsis") or sha256(content) != s.get("synopsis_hash") or s["id"] in touched)
        if outdated:
            parents.append({"id": s["id"], "title": s.get("title", ""), "depth": depth(s),
                            "children": [{"id": c["id"], "title": c.get("title", ""), "synopsis": c["synopsis"]} for c in children]})
    parents.sort(key=lambda p: -p["depth"])  # the deepest first: a parent's children are settled before it
    top = [s for s in by_parent.get(None, []) if s.get("synopsis")]
    doc_needed = bool(top) and (not doc.get("synopsis") or bool(touched))
    return {
        "document": {"id": doc.get("id"), "title": doc.get("title", ""), "synopsis": doc.get("synopsis") or ""},
        "leaves_to_write": leaves,
        "parents_to_roll_up": parents,
        "document_synopsis_needed": doc_needed,
        "top_level_synopses": [{"id": s["id"], "title": s.get("title", ""), "synopsis": s["synopsis"]} for s in top] if doc_needed else [],
    }


def has_work(report: dict[str, Any]) -> bool:
    return bool(report["leaves_to_write"] or report["parents_to_roll_up"] or report["document_synopsis_needed"])


async def report_for(doc_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        doc = await _get(client, f"/documents/{doc_id}")
        sections = await _get(client, f"/documents/{doc_id}/sections")
    return analyse(doc, sections, _touched.get(doc_id))


async def set_section_synopsis(doc_id: str, section_id: str, synopsis: str) -> dict[str, Any]:
    """Stores the synopsis with the hash of the section's content as it is now, and notes that what is above it must be
    rolled up again."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        sections = await _get(client, f"/documents/{doc_id}/sections")
        section = next((s for s in sections if s["id"] == section_id), None)
        if section is None:
            raise RuntimeError(f"Not found: section {section_id} of document {doc_id}")
        if section.get("locked_by"):
            raise RuntimeError(f"Section {section_id} is locked by {section['locked_by']}; leave it for now.")
        resp = await client.patch(f"{_base()}/documents/{doc_id}/sections/{section_id}/synopsis",
                                  json={"synopsis": synopsis, "synopsis_hash": sha256(section.get("content") or "")})
        resp.raise_for_status()
    touched = _touched.setdefault(doc_id, set())
    touched.discard(section_id)  # its own roll-up is what this write was, if it was a parent
    parent = section.get("parent_section_id")
    while parent:
        touched.add(parent)
        up = next((s for s in sections if s["id"] == parent), None)
        parent = up.get("parent_section_id") if up else None
    return {"section_id": section_id, "stored": True}


async def set_document_synopsis(doc_id: str, synopsis: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.patch(f"{_base()}/documents/{doc_id}/meta", json={"synopsis": synopsis})
        resp.raise_for_status()
    _touched.pop(doc_id, None)
    return {"doc_id": doc_id, "stored": True}


# --- events -> one turn per document ---------------------------------------------------------------------------------------

def event_doc_id(msg: dict[str, Any]) -> str | None:
    payload = msg.get("payload") or {}
    return payload.get("doc_id") or payload.get("document_id") or None


def configure(starter: Callable[[str], Awaitable[str | None]]) -> None:
    """starter(doc_id) starts the document's turn and returns its chain id (None: no turn)."""
    global _starter
    _starter = starter


def note_event(doc_id: str) -> None:
    _last[doc_id] = time.monotonic()
    if doc_id in _running:
        _dirty.add(doc_id)
    elif doc_id not in _waiting:
        _waiting[doc_id] = asyncio.get_running_loop().create_task(_after_quiet(doc_id))


async def _after_quiet(doc_id: str) -> None:
    try:
        while time.monotonic() - _last.get(doc_id, 0) < quiet_seconds():
            await asyncio.sleep(1)
        if doc_id in _running:
            _dirty.add(doc_id)
            return
        if _starter is None:
            return
        report = await report_for(doc_id)
        if not has_work(report):
            log_event("plugin:akinakes-maintainer", "nothing_to_do", doc_id=doc_id)
            return
        chain_id = await _starter(doc_id)
        if chain_id:
            _running[doc_id] = chain_id
    except Exception as exc:  # noqa: BLE001 -- a document that cannot be read now is taken at its next event
        log_event("plugin:akinakes-maintainer", "start_failed", doc_id=doc_id, error=str(exc))
    finally:
        _waiting.pop(doc_id, None)


def turn_ended(chain_id: str) -> None:
    """The channel's turn for chain_id is over: events that came meanwhile make one more turn."""
    for doc_id, running in list(_running.items()):
        if running == chain_id:
            _running.pop(doc_id, None)
            if doc_id in _dirty:
                _dirty.discard(doc_id)
                note_event(doc_id)

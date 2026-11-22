"""Tools of the akinakes document maintainer (AGENT_ROLE=maintainer, DOCUMENT_SERVICE_URL set): what is out of date in a
document's synopses, and the writing of a section's and of the document's synopsis. No other Caroline user or akinakes
role gets them. The logic is in app/akinakes_maintainer.py."""

from __future__ import annotations

import os
from typing import Any

from app import akinakes_maintainer as m
from app.plugins.document_service_plugin import _base, _failure, _reply, _tool
from app.plugins.loader import Plugin


async def maintainer_pending(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _reply(await m.report_for(str(args["doc_id"])))
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)


async def maintainer_set_section_synopsis(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    synopsis = str(args.get("synopsis") or "").strip()
    if not synopsis:
        return _failure(RuntimeError("The synopsis is empty."))
    try:
        return _reply(await m.set_section_synopsis(str(args["doc_id"]), str(args["section_id"]), synopsis))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def maintainer_set_document_synopsis(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    synopsis = str(args.get("synopsis") or "").strip()
    if not synopsis:
        return _failure(RuntimeError("The synopsis is empty."))
    try:
        return _reply(await m.set_document_synopsis(str(args["doc_id"]), synopsis))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


_DOC = {"doc_id": {"type": "string"}}

PLUGIN = None if not _base() or (os.environ.get("AGENT_ROLE") or "").strip() != "maintainer" else Plugin(
    name="akinakes_maintainer",
    description="The synopses of a document's sections and of the document",
    always_loaded=True,
    tools=[
        _tool("maintainer_pending", "What is out of date in a document's synopses: leaves_to_write (sections with their "
              "content), parents_to_roll_up (deepest first, with their children's synopses), and whether the document's own "
              "synopsis is needed (with the top-level synopses). Call it again after writing: a parent is listed only when "
              "all its children have a synopsis.", _DOC, ["doc_id"], maintainer_pending),
        _tool("maintainer_set_section_synopsis", "Stores the synopsis of one section (a leaf from its content, a parent from "
              "its children's synopses).", {**_DOC, "section_id": {"type": "string"}, "synopsis": {"type": "string"}},
              ["doc_id", "section_id", "synopsis"], maintainer_set_section_synopsis),
        _tool("maintainer_set_document_synopsis", "Stores the synopsis of the whole document.",
              {**_DOC, "synopsis": {"type": "string"}}, ["doc_id", "synopsis"], maintainer_set_document_synopsis),
    ],
)

"""The file of a binary document (docx, xlsx, pdf) for the akinakes document creator: Document Service renders the
document in the format (GET /documents/{id}/export) and the file is stored as the document's file (POST
/documents/{id}/file), as the old doc_creator agent did. Only in a role started with AGENT_ROLE=doc_creator and
DOCUMENT_SERVICE_URL, so no other Caroline user or akinakes role gets this tool."""

from __future__ import annotations

import io
import os
from typing import Any

import httpx

from app.plugins.document_service_plugin import TIMEOUT_S, _base, _failure, _reply, _tool
from app.plugins.loader import Plugin

FORMATS = ("docx", "xlsx", "pdf")


async def document_render_file(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    doc_id, fmt = str(args["doc_id"]), str(args["format"]).lower()
    if fmt not in FORMATS:
        return _failure(RuntimeError(f"A file is made only for {', '.join(FORMATS)}; this format is {fmt!r}."))
    base = _base()
    try:
        async with httpx.AsyncClient(timeout=max(TIMEOUT_S, 60)) as client:
            exported = await client.get(f"{base}/documents/{doc_id}/export", params={"format": fmt})
            exported.raise_for_status()
            stored = await client.post(f"{base}/documents/{doc_id}/file",
                                       files={"file": (f"{doc_id}.{fmt}", io.BytesIO(exported.content), "application/octet-stream")})
            stored.raise_for_status()
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)
    return _reply({"doc_id": doc_id, "format": fmt, "bytes": len(exported.content)})


PLUGIN = None if not _base() or (os.environ.get("AGENT_ROLE") or "").strip() != "doc_creator" else Plugin(
    name="akinakes_file",
    description="The file of a docx, xlsx or pdf document",
    always_loaded=True,
    tools=[
        _tool("document_render_file", "Renders the document as a file of the format (docx, xlsx or pdf) and stores it as the "
              "document's file. Only for docx, xlsx and pdf (a markdown, html, json or text document has no separate file). "
              "Call it after the document's sections are in place.",
              {"doc_id": {"type": "string"}, "format": {"type": "string", "enum": list(FORMATS)}}, ["doc_id", "format"],
              document_render_file),
    ],
)

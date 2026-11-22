"""The research role's corpus: ingest a paper into a room's corpus, and semantic search over it. Wraps akinakes' own
agents/research/{ingest,query}.py (Chroma + llama-index, embeddings through Camerlengo) instead of a copy of them
(owner's decision, 2026-10-31: the whole research agent inside Caroline). AKINAKES_RESEARCH_DIR is where that code is
in the image; the corpus lives under RESEARCH_DATA_DIR (the agent's workspace volume). Not loaded when
AKINAKES_RESEARCH_DIR is not set."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from typing import Any

from app.plugins.document_service_plugin import _failure, _reply, _tool
from app.plugins.loader import Plugin


def _research_dir() -> str:
    return (os.environ.get("AKINAKES_RESEARCH_DIR") or "").strip()


def _module(name: str) -> Any:
    """agents/research's modules import each other by bare name (camerlengo_embedding, corpus_store): their directory
    goes on sys.path, as agents/research/main.py puts it there."""
    path = _research_dir()
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module(name)


async def research_ingest_paper(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        ingest = _module("ingest")
        result = await asyncio.to_thread(ingest.ingest_paper, str(args["file_url"]), str(args["room_id"]),
                                         str(args.get("caller_email") or ""), args.get("title") or None)
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)
    return _reply(result)


async def research_query(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        query = _module("query")
        hits = await asyncio.to_thread(query.query_corpus, str(args["query"]), str(args["room_id"]), int(args.get("top_k") or 5))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return _reply(hits)


_STR = {"type": "string"}

# Only where agents/research is in the image (AKINAKES_RESEARCH_DIR set): elsewhere the plugin is not loaded at all.
PLUGIN = None if not _research_dir() else Plugin(
    name="research_corpus",
    description="A room's research corpus: ingest papers, search them",
    tools=[
        _tool("research_ingest_paper", "Downloads a paper attached in the room (its file_url), splits and embeds it, and adds "
              "it to the room's corpus. Returns the corpus document and the number of chunks indexed.",
              {"file_url": _STR, "room_id": _STR, "caller_email": _STR, "title": _STR}, ["file_url", "room_id"],
              research_ingest_paper),
        _tool("research_query", "Semantic search over the room's corpus: the passages closest to the query, with their papers.",
              {"query": _STR, "room_id": _STR, "top_k": {"type": "integer"}}, ["query", "room_id"], research_query),
    ],
)

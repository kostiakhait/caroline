"""reforce -- Wikipedia search/reading, fact extraction, and source indexing/search,
all run server-side by the beautysqrl.com AI service (Reforce's ai:* commands).

Everything here is a thin client: no nltk/spacy, no local index, no local
Wikipedia cache. The server does the work and returns the result; this module
only shapes the request and formats the answer for the model.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.reforce_v2 import call as _call

_SOURCE_TYPES = {"pdf", "txt", "docx", "pptx", "xlsx", "md", "html", "csv"}


async def wikipedia_search(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"query": args["query"], "lang": args.get("lang") or "en", "limit": int(args.get("limit") or 10)}
    data = await _call("ai:wikipediaSearch", body)
    results = data.get("results") or []
    if not results:
        return {"text": f"No Wikipedia articles found for {args['query']!r}."}
    lines = [f"{i + 1}. {r['title']} -- {r.get('extract', '')} ({r.get('url', '')})" for i, r in enumerate(results)]
    return {"text": "\n".join(lines)}


async def wikipedia_get_article(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"title": args["title"], "lang": args.get("lang") or "en"}
    if args.get("maxChars"):
        body["maxChars"] = int(args["maxChars"])
    data = await _call("ai:wikipediaArticle", body)
    if not data.get("found"):
        return {"text": f"No Wikipedia article titled {args['title']!r} was found."}
    note = " [truncated -- ask for a larger maxChars if you need more]" if data.get("truncated") else ""
    return {"text": f"{data.get('title')} ({data.get('url', '')}){note}\n\n{data.get('content', '')}"}


async def extract_facts(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    source = args["source"]
    body: dict[str, Any] = {"query": args["query"]}
    if source == "wikipedia":
        body["wikipedia"] = True
        body["lang"] = args.get("lang") or "en"
    elif source == "text":
        body["text"] = args["text"]
    elif source == "uid":
        body["uid"] = args["uid"]
        body["type"] = args["type"]
    else:
        raise ValueError("source must be one of: wikipedia, text, uid")
    body["maxParagraphs"] = int(args.get("maxParagraphs") or 20)
    if args.get("minRelevance") is not None:
        body["minRelevance"] = float(args["minRelevance"])
    data = await _call("ai:factExtract", body)
    facts = data.get("facts") or []
    if not facts:
        return {"text": "No facts met the relevance threshold for this query."}
    lines = [f"- {f.get('fact', '')} (relevance {f.get('relevance')}; source: {f.get('where')})" for f in facts]
    return {"text": "\n".join(lines)}


async def index_source(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    path = Path(args["path"])
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    src_type = (args.get("type") or path.suffix.lstrip(".")).lower()
    if src_type not in _SOURCE_TYPES:
        raise ValueError(f"Unsupported source type {src_type!r}; expected one of {sorted(_SOURCE_TYPES)}")
    content = base64.b64encode(path.read_bytes()).decode("ascii")
    data = await _call("ai:loadSourceSync", {"type": src_type, "content": content})
    uid = (data.get("document") or {}).get("uid")
    if not uid:
        raise RuntimeError("ai:loadSourceSync returned no document uid")
    log_event("plugin:reforce", "source_indexed", type=src_type, uid=uid)
    return {"text": f"Indexed {path.name} as uid {uid} (type {src_type}). Use this uid with search_sources."}


async def search_sources(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    sources = args["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty list of {uid, type} objects")
    body: dict[str, Any] = {"query": args["query"], "sources": sources}
    if args.get("maxAnswers") is not None:
        body["maxAnswers"] = int(args["maxAnswers"])
    if args.get("maxP") is not None:
        body["maxP"] = int(args["maxP"])
    data = await _call("ai:aspectResearch", body)
    payload = {k: v for k, v in data.items() if not k.startswith(".")}
    return {"text": json.dumps(payload, ensure_ascii=False, indent=2)}


PLUGIN = Plugin(
    name="reforce",
    usage_instructions=(
        "Use wikipedia_search to find candidate articles, then wikipedia_get_article to read one in full before "
        "relying on it. extract_facts pulls concrete, sourced facts for a query from Wikipedia, pasted text, or an "
        "already-indexed source (uid). To work with a document the user gave you: index_source once to get its uid, "
        "then search_sources to answer questions across one or more indexed sources. Each extract_facts call costs "
        "server-side model calls, so keep maxParagraphs modest unless the user asks for a broad survey."
    ),
    tools=[
        PluginTool(
            "wikipedia_search",
            "Searches Wikipedia and returns candidate article titles with short extracts and URLs.",
            {"query": str, "lang": str | None, "limit": int | None},
            wikipedia_search,
        ),
        PluginTool(
            "wikipedia_get_article",
            "Returns the full plain-text content of one Wikipedia article (truncated to maxChars).",
            {"title": str, "lang": str | None, "maxChars": int | None},
            wikipedia_get_article,
        ),
        PluginTool(
            "extract_facts",
            "Extracts concrete facts relevant to a query from a source: 'wikipedia' (optionally with lang), "
            "'text' (pass text), or 'uid' (pass uid and type of an already-indexed source). Each fact comes with a "
            "relevance score and where it came from.",
            {
                "source": str,
                "query": str,
                "lang": str | None,
                "text": str | None,
                "uid": str | None,
                "type": str | None,
                "maxParagraphs": int | None,
                "minRelevance": float | None,
            },
            extract_facts,
        ),
        PluginTool(
            "index_source",
            "Uploads a local document (pdf, txt, docx, pptx, xlsx, md, html, csv) to be processed server-side. "
            "Returns its uid for later use with extract_facts or search_sources. Waits until processing finishes.",
            {"path": str, "type": str | None},
            index_source,
        ),
        PluginTool(
            "search_sources",
            "Answers a question by searching across one or more already-indexed sources. Each source is an object "
            "{uid, type} as returned by index_source.",
            {
                "query": str,
                "sources": list,
                "maxAnswers": int | None,
                "maxP": int | None,
            },
            search_sources,
        ),
    ],
)

"""wikipedia -- Wikipedia search and articles through Camerlengo (2026-10-24, the
user's request: "В Камерленго уже есть поиск по википедии. Я хочу чтобы он стал
инструментом Кэролайн").

  wikipedia_search(query, lang)   ai:wikipediaSearch -- titles, addresses, the
                                  first sentences of each article
  wikipedia_read(title, lang)     ai:wikipediaArticle -- the article's text,
                                  `offset` continues a long one
  wikipedia_facts(question, lang) ai:factExtract over Wikipedia -- facts the
                                  server's own extractor finds; PAID, slow

search and read are plain MediaWiki API calls on the server -- no model, no
charge (checked 2026-10-24 in API/Api2AICommands.py). facts runs the server's
FactExtractor: about 40 gpt-5-nano calls, $0.036 and 4 minutes for one question
(measured 2026-10-24), charged to the user's wallet, so it goes under the user's
session. The text is Wikipedia's, so it goes out as
page data between untrusted-text markers, like web_read's. Not part of
web_research (the user, 2026-10-24: "Из Веб ресерч исключить")."""

from __future__ import annotations

import time
from typing import Any

from app.context_cap import cap_count, truncate
from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.plugins.sw_api import CAROLINE_SW_KEY, SessionExpiredError, SessionManager, SwApiError, _post_json, call_v2
from app.untrusted_text import web_pages_text

# ai:wikipediaArticle's own ceiling on the text it returns (Api2AICommands.py); the
# whole of it is asked for and handed out in parts here, so nothing is cut.
_ARTICLE_MAX_CHARS = 100_000
# How long wikipedia_facts waits for the server: the same 600 s the user set on the
# squirrelwisdom.com proxy for it (2026-10-24; the old 120 s cut its 4-minute answer off).
_FACTS_TIMEOUT_S = 600.0
_sessions = SessionManager()


def _lang(args: dict[str, Any]) -> str:
    return str(args.get("lang") or "en").strip().lower() or "en"


async def wikipedia_search(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"text": 'wikipedia_search needs "query".', "is_error": True}
    lang = _lang(args)
    try:
        envelope = await call_v2("ai:wikipediaSearch", key=CAROLINE_SW_KEY, query=query, lang=lang)
    except SwApiError as exc:
        log_event("plugin:wikipedia", "search_failed", lang=lang, query=query[:200], error=str(exc)[:300])
        return {"text": f"Wikipedia search failed: {exc}", "is_error": True}
    results, _cut = cap_count(envelope.get("results") or [])
    log_event("plugin:wikipedia", "search_done", lang=lang, query=query[:200], count=len(results),
              titles=[r.get("title") for r in results])
    if not results:
        return {"text": f"Wikipedia ({lang}) has no article for {query!r}."}
    lines = [f"{i}. {r.get('title')} -- {r.get('url')}" + (f"\n   {r['extract']}" if r.get("extract") else "")
             for i, r in enumerate(results, start=1)]
    return {"text": f"Wikipedia ({lang}) results for {query!r}:\n" + web_pages_text("\n".join(lines))}


async def wikipedia_read(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    if not title:
        return {"text": 'wikipedia_read needs "title" (as wikipedia_search gives it).', "is_error": True}
    lang = _lang(args)
    try:
        envelope = await call_v2("ai:wikipediaArticle", key=CAROLINE_SW_KEY, title=title, lang=lang, maxChars=_ARTICLE_MAX_CHARS)
    except SwApiError as exc:
        log_event("plugin:wikipedia", "read_failed", lang=lang, title=title[:200], error=str(exc)[:300])
        return {"text": f"Reading the Wikipedia article failed: {exc}", "is_error": True}
    if not envelope.get("found"):
        log_event("plugin:wikipedia", "read_not_found", lang=lang, title=title[:200])
        return {"text": f"Wikipedia ({lang}) has no article titled {title!r} -- use the exact title from wikipedia_search."}
    content = str(envelope.get("content") or "")
    offset = int(args.get("offset") or 0)
    text, more = truncate(content, offset=offset)
    log_event("plugin:wikipedia", "read_done", lang=lang, title=title[:200], chars=len(content), offset=offset,
              more=bool(more), cut_by_server=bool(envelope.get("truncated")))
    tail = []
    if more:
        tail.append(more)
    elif envelope.get("truncated"):
        tail.append(f"(The article is longer than the {_ARTICLE_MAX_CHARS} characters Wikipedia's text is given out in; "
                    f"the rest is at {envelope.get('url')}, readable with web_read.)")
    body = f"{title} -- {envelope.get('url')}\n\n{text}" + ("\n\n" + "\n".join(tail) if tail else "")
    return {"text": web_pages_text(body)}


async def wikipedia_facts(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    question = str(args.get("question") or "").strip()
    if not question:
        return {"text": 'wikipedia_facts needs "question".', "is_error": True}
    lang = _lang(args)
    started = time.monotonic()

    async def call(session: str) -> dict[str, Any]:
        # One attempt only: a dropped connection must not start a second paid 4-minute run.
        envelope = await _post_json({"command": "ai:factExtract", "key": CAROLINE_SW_KEY, "session": session,
                                     "query": question, "wikipedia": True, "lang": lang},
                                    timeout=_FACTS_TIMEOUT_S, retries=1)
        if envelope.get(".status") != "ok":
            reason = str(envelope.get(".reason") or envelope)
            if "session" in reason.lower():
                raise SessionExpiredError(reason)
            raise SwApiError(f'ai:factExtract failed: {reason}')
        return envelope

    log_event("plugin:wikipedia", "facts_started", lang=lang, question=question[:200])
    try:
        envelope = await _sessions.with_session(call)
    except Exception as exc:  # noqa: BLE001 -- reported to the model, logged
        log_event("plugin:wikipedia", "facts_failed", lang=lang, error=str(exc)[:300], seconds=round(time.monotonic() - started, 1))
        return {"text": f"Wikipedia fact extraction failed: {exc}", "is_error": True}
    facts, _cut = cap_count(envelope.get("facts") or [])
    log_event("plugin:wikipedia", "facts_done", lang=lang, count=len(facts), seconds=round(time.monotonic() - started, 1))
    if not facts:
        return {"text": f"Wikipedia ({lang}) fact extraction found nothing for {question!r}."}
    lines = []
    for i, item in enumerate(facts, start=1):
        where = f" (from: {item['where']})" if item.get("where") else ""
        relevance = f" [relevance {item['relevance']}]" if item.get("relevance") is not None else ""
        lines.append(f"{i}. {item.get('fact')}{where}{relevance}")
    return {"text": f"Facts from Wikipedia ({lang}) for {question!r}:\n" + web_pages_text("\n".join(lines))}


PLUGIN = Plugin(
    name="wikipedia",
    tools=[
        PluginTool(
            "wikipedia_search",
            "Searches Wikipedia: article titles, addresses and the first sentences of each. `lang` is the "
            "Wikipedia language code (\"en\" default, \"ru\", \"de\", ...) -- search the Wikipedia of the "
            "language the topic is best covered in. Free, no model involved.",
            {"query": str, "lang": str | None}, wikipedia_search,
        ),
        PluginTool(
            "wikipedia_read",
            "Reads one Wikipedia article's text. `title` exactly as wikipedia_search gives it, `lang` the same "
            "language code. `offset` continues a long article from where the previous reply stopped. Free.",
            {"title": str, "lang": str | None, "offset": int | None}, wikipedia_read,
        ),
        PluginTool(
            "wikipedia_facts",
            "Has the server pick out the facts Wikipedia states about a question (it chooses the articles "
            "itself). PAID and SLOW: about $0.04 of the user's money and up to 4 minutes per question -- use it "
            "only when a question needs facts gathered from many articles at once; for one known topic, "
            "wikipedia_search + wikipedia_read are free and faster. `lang`: the Wikipedia language code (\"en\" "
            "default).",
            {"question": str, "lang": str | None}, wikipedia_facts,
        ),
    ],
)

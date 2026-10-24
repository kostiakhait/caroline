"""web_research -- the agentic web search loop (designed with the user 2026-10-23).

    plan -> search (every query on its engines, at once) -> pick -> read + extract
    (every page at once, every part of a page at once) -> judge -> answer

Event-driven, not round by round: pages are read and their facts extracted as
they arrive; the judge looks at the facts each time new ones came in; when it
says something is missing, the next round's queries go out at once, without
waiting for the slow pages of the previous one (those still add their facts).
Links a page points to, when its extractor says they are worth following, are
read too.

Stops when the judge says the facts answer the question, when a finished round
added no fact at all, when nothing more could be searched, or after
RESEARCH_TIME_LIMIT_S (the user's 1.5 minutes) -- then the answer is written
from what was gathered and says it may be incomplete.

Every fact carries a verbatim quote of its page; a quote that is not in the
page text (compared ignoring case and whitespace) is dropped with its fact
before anyone sees it -- the model cannot put words into a source's mouth.

The browser is app/web_browser.py (at most 5 tabs at once, enforced there);
the agents are app/microagents/web_research_agents.py, run through the local
microagent ladder with the model calls going through SquirrelWisdom.
Everything is logged under "plugin:web"."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.context_cap import ITEM_MAX_CHARS, cap_count
from app.logging_setup import log_event
from app import web_browser
from app.web_browser import ENGINES, SearchResult, WebBrowserError

RESEARCH_TIME_LIMIT_S = 90.0   # the user's limit (2026-10-23): one web_research call


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def quote_in_text(quote: str, text: str) -> bool:
    """A quote counts only when it really is in the page: same characters,
    whitespace and case aside."""
    wanted = _normalized(quote).strip(" .\"'«»“”")
    return bool(wanted) and wanted in _normalized(text)


def chunks_of(text: str, size: int = ITEM_MAX_CHARS) -> list[str]:
    """The page's text in parts the extractor reads one at a time (every part
    at the same time), cut at paragraph breaks where there is one."""
    parts, rest = [], text.strip()
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


@dataclass
class Fact:
    label: str
    statement: str
    quote: str
    url: str
    title: str


@dataclass
class ResearchResult:
    question: str
    answer: str
    facts: list[Fact]              # the facts the answer cites, in citation order
    incomplete: bool
    stopped_because: str
    queries: list[str]
    pages_read: int
    seconds: float
    all_facts: int = 0


@dataclass
class _State:
    question: str
    today: str
    facts: list[Fact] = field(default_factory=list)
    quotes: set[str] = field(default_factory=set)
    tried: list[str] = field(default_factory=list)
    urls: set[str] = field(default_factory=set)
    pages_read: int = 0


class _Agents:
    """The web.* agents on one local runtime, called from the event loop."""

    def __init__(self) -> None:
        from app.microagent_client import build_local_runtime, new_budget
        from app.microagents import web_research_agents
        self._new_budget = new_budget
        self.runtime = build_local_runtime()
        if self.runtime is None:
            raise WebBrowserError("the microagent runtime is not available in this build -- agentic web search cannot run")
        for agent in web_research_agents.llm_agents():
            self.runtime.register(agent)

    async def run(self, name: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any] | None:
        """The agent's output; {} when it found nothing ("empty"); None when it
        failed or did not finish in time (logged)."""
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.runtime.run, name, payload, self.runtime.new_context(budget=self._new_budget())),
                timeout=max(0.0, timeout_s),
            )
        except asyncio.TimeoutError:
            log_event("plugin:web", "agent_timeout", agent=name, ms=int((time.monotonic() - started) * 1000))
            return None
        status = (result or {}).get("status")
        log_event("plugin:web", "agent_done", agent=name, status=status, error=(result or {}).get("error"),
                  ms=int((time.monotonic() - started) * 1000))
        if status == "ok":
            return (result or {}).get("output") or {}
        if status == "empty":
            return {}
        return None


async def find(query: str, engines: list[str], timeout_s: float) -> tuple[list[SearchResult], list[str]]:
    """Results of `query` on each of `engines`, all at once; when every one of
    them failed, the other engines one by one until one answers. Returns the
    results and the engines that answered."""
    async def one(engine: str) -> tuple[str, list[SearchResult] | None]:
        try:
            return engine, await web_browser.search(engine, query, timeout_s)
        except WebBrowserError as exc:
            log_event("plugin:web", "engine_failed", engine=engine, query=query[:200], error=str(exc)[:300])
            return engine, None

    answered = await asyncio.gather(*(one(e) for e in engines))
    results = [r for _engine, rows in answered if rows for r in rows]
    used = [engine for engine, rows in answered if rows]
    if not used:
        for engine in (e for e in ENGINES if e not in engines):
            _engine, rows = await one(engine)
            if rows:
                log_event("plugin:web", "engine_fallback", query=query[:200], failed=engines, used=engine)
                return rows, [engine]
    return results, used


async def research(question: str, language: str | None = None,
                   progress: Callable[[dict[str, Any]], Any] | None = None) -> ResearchResult:
    started = time.monotonic()
    deadline = started + RESEARCH_TIME_LIMIT_S
    left = lambda: deadline - time.monotonic()   # noqa: E731
    agents = _Agents()
    state = _State(question=question, today=time.strftime("%A, %Y-%m-%d"))
    log_event("plugin:web", "research_started", question=question[:300], limit_s=RESEARCH_TIME_LIMIT_S)

    def report(**data: Any) -> None:
        if progress is not None:
            try:
                progress({**data, "facts": len(state.facts), "pagesRead": state.pages_read, "queries": len(state.tried)})
            except Exception:  # noqa: BLE001 -- progress is a courtesy
                pass

    async def plan_and_search(round_no: int, missing: str) -> list[SearchResult]:
        """One round's queries, searched at once, and the results worth opening."""
        plan = await agents.run("web.plan", {"question": question, "today": state.today, "missing": missing, "tried": state.tried}, left())
        queries = [q for q in (plan or {}).get("queries") or [] if _normalized(q["query"]) not in {_normalized(t) for t in state.tried}]
        log_event("plugin:web", "round_planned", round=round_no, queries=[(q["query"][:200], q["engines"]) for q in queries])
        if not queries:
            return []
        state.tried.extend(q["query"] for q in queries)
        report(stage="searching", round=round_no)
        found = await asyncio.gather(*(find(q["query"], list(q["engines"]), left()) for q in queries))
        fresh: list[SearchResult] = []
        for results, _used in found:
            for result in results:
                if result.url not in state.urls and all(result.url != f.url for f in fresh):
                    fresh.append(result)
        if not fresh:
            return []
        fresh, _cut = cap_count(fresh)
        labeled = [{"label": str(i), "title": r.title, "url": r.url, "snippet": r.snippet} for i, r in enumerate(fresh, start=1)]
        picked = await agents.run("web.pick", {"question": question, "results": labeled}, left())
        chosen = [fresh[int(label) - 1] for label in (picked or {}).get("open") or []]
        log_event("plugin:web", "round_picked", round=round_no, offered=len(fresh), chosen=[r.url for r in chosen])
        return chosen

    async def read_and_extract(url: str) -> tuple[int, list[str]]:
        """Reads one page and adds its quoted facts; returns how many facts
        were added and the links worth following."""
        state.urls.add(url)
        try:
            page = await web_browser.read(url, left())
        except (WebBrowserError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 -- one bad page never stops the search
            log_event("plugin:web", "page_skipped", url=url[:300], error=str(exc)[:300])
            return 0, []
        state.pages_read += 1
        report(stage="reading", url=url)
        if not page.text.strip():
            return 0, []
        links_seen, links = set(), []
        for link in page.links:
            if link["url"] not in links_seen and link["url"] not in state.urls:
                links_seen.add(link["url"])
                links.append(link)
        links, _cut = cap_count(links)
        labeled_links = [{"label": str(i), "text": link["text"], "url": link["url"]} for i, link in enumerate(links, start=1)]
        parts = chunks_of(page.text)
        outputs = await asyncio.gather(*(
            agents.run("web.extract", {
                "question": question, "url": page.url, "title": page.title, "text": part,
                **({"part": index} if len(parts) > 1 else {}),
                "links": labeled_links if index == 1 else [],
            }, left())
            for index, part in enumerate(parts, start=1)
        ))
        added, dropped, follow = 0, 0, []
        for output, part in zip(outputs, parts):
            for fact in (output or {}).get("facts") or []:
                quote = str(fact["quote"]).strip()
                if not quote_in_text(quote, part):
                    dropped += 1
                    continue
                key = _normalized(quote)
                if key in state.quotes:
                    continue
                state.quotes.add(key)
                state.facts.append(Fact(label=str(len(state.facts) + 1), statement=str(fact["statement"]).strip(),
                                        quote=quote, url=page.url, title=page.title))
                added += 1
            for label in (output or {}).get("follow") or []:
                follow.append(labeled_links[int(label) - 1]["url"])
        log_event("plugin:web", "page_extracted", url=url[:300], parts=len(parts), facts_added=added,
                  quotes_not_in_page=dropped, follow=follow)
        return added, follow

    tasks: dict[asyncio.Task, tuple[str, int]] = {}
    open_in_round: dict[int, int] = {}
    added_in_round: dict[int, int] = {}
    finished_rounds: list[int] = []
    rounds = 0
    judged_at = 0
    judging = planning = False
    missing = ""
    stopped_because = ""
    incomplete = False

    def start(kind: str, round_no: int, coroutine: Any) -> None:
        tasks[asyncio.ensure_future(coroutine)] = (kind, round_no)
        open_in_round[round_no] = open_in_round.get(round_no, 0) + 1

    def start_round() -> None:
        nonlocal rounds, planning
        rounds += 1
        planning = True
        added_in_round[rounds] = 0
        start("round", rounds, plan_and_search(rounds, missing))

    start_round()
    try:
        while True:
            if not tasks:
                # Nothing in flight: one more round, unless the last one found nothing new.
                if finished_rounds and added_in_round.get(finished_rounds[-1], 0) == 0:
                    stopped_because = "the last round found no new facts"
                    break
                if left() <= 0:
                    stopped_because, incomplete = "time limit", True
                    break
                start_round()
            done, _pending = await asyncio.wait(list(tasks), timeout=max(0.0, left()), return_when=asyncio.FIRST_COMPLETED)
            if not done:
                stopped_because, incomplete = "time limit", True
                break
            enough = False
            for task in done:
                kind, round_no = tasks.pop(task)
                open_in_round[round_no] -= 1
                try:
                    value = task.result()
                except Exception as exc:  # noqa: BLE001
                    log_event("plugin:web", "research_step_failed", kind=kind, round=round_no, error=str(exc)[:300])
                    value = None
                if kind == "round":
                    planning = False
                    for result in value or []:
                        if result.url not in state.urls:
                            state.urls.add(result.url)
                            start("read", round_no, read_and_extract(result.url))
                elif kind == "read" and value:
                    added, follow = value
                    added_in_round[round_no] = added_in_round.get(round_no, 0) + added
                    for url in follow:
                        if url not in state.urls:
                            state.urls.add(url)
                            start("read", round_no, read_and_extract(url))
                elif kind == "judge":
                    judging = False
                    verdict = value or {}
                    log_event("plugin:web", "judged", facts=judged_at, enough=verdict.get("enough"), missing=str(verdict.get("missing") or "")[:300])
                    if verdict.get("enough"):
                        enough = True
                    elif verdict.get("missing"):
                        missing = str(verdict["missing"])
                        if not planning:
                            start_round()
                if open_in_round.get(round_no) == 0 and kind != "judge" and round_no not in finished_rounds:
                    finished_rounds.append(round_no)
                    log_event("plugin:web", "round_finished", round=round_no, facts_added=added_in_round.get(round_no, 0))
            if enough:
                stopped_because = "the facts answer the question"
                break
            if len(state.facts) > judged_at and not judging:
                judged_at = len(state.facts)
                judging = True
                report(stage="judging")
                start("judge", 0, agents.run("web.judge", {
                    "question": question, "today": state.today,
                    "facts": [{"label": f.label, "statement": f.statement, "url": f.url} for f in state.facts],
                }, left()))
            if left() <= 0:
                stopped_because, incomplete = "time limit", True
                break
    finally:
        for task in tasks:
            task.cancel()

    answer_text, cited = "", []
    if state.facts:
        report(stage="answering")
        answer = await agents.run("web.answer", {
            "question": question, "today": state.today, "language": language or "", "incomplete": incomplete,
            "facts": [{"label": f.label, "statement": f.statement, "url": f.url} for f in state.facts],
        }, RESEARCH_TIME_LIMIT_S)
        if answer:
            answer_text = str(answer["answer"]).strip()
            by_label = {f.label: f for f in state.facts}
            cited = [by_label[label] for label in dict.fromkeys(answer.get("used") or []) if label in by_label]
    seconds = time.monotonic() - started
    log_event("plugin:web", "research_done", stopped_because=stopped_because, incomplete=incomplete, rounds=rounds,
              queries=len(state.tried), pages_read=state.pages_read, facts=len(state.facts), cited=len(cited),
              answered=bool(answer_text), seconds=round(seconds, 1))
    return ResearchResult(question=question, answer=answer_text, facts=cited, incomplete=incomplete,
                          stopped_because=stopped_because, queries=list(state.tried), pages_read=state.pages_read,
                          seconds=seconds, all_facts=len(state.facts))

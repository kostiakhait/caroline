"""Client-side counterpart of reforce's API/Api2Evolution.py::evolve_children,
for the `reply:` domain only. Per explicit instruction (2026-10-18):
Camerlengo is a universal server -- no product-specific application logic
may EXECUTE on it. The `reply:` domain (PermissionGuard, ReplyReview,
RefusalGuard, ActionSummary, ReplyExpected, BackgroundRelayWorth,
ContentSummary, ReplyTranslate -- all Caroline's own business rules) now
executes entirely here; Camerlengo only ever sees the generic, product-
agnostic `evolution:resolveCalls`/`evolution:mintClone`/`evolution:
recordOutcome` (evolution bookkeeping/management -- this module's own
server-side counterpart, API/Api2Evolution.py's resolve_calls/mint_clone/
record_outcome) and `microagent:call` (the one real model-call primitive,
see app/microagent_client.py) commands. The `memory:` domain stays
server-executed permanently -- genuinely cross-product shared
infrastructure, not touched by this change.

Mirrors evolve_children's own logic exactly (same dice roll already applied
server-side in resolve_calls, same A/B randomization, same promotion/cull
bookkeeping via record_outcome) -- the only difference is WHERE the real
model calls happen: there, in-process on the server; here, in-process on
this client, via app/microagent_client.py's build_local_runtime() and
Camerlengo's generic microagent:call primitive underneath.

Every call site that used to POST a `reply:*` command calls run_reply_agent
instead."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

from app.evolvable_roles_client import EVOLVABLE_ROLES, ladder_from_json, ladder_to_json, resolve_agent_for_clone, seed_genome
from app.logging_setup import log_event
from app.microagent_client import Microagent as M, build_local_runtime, new_budget
from app.microagents import mutator, selector
from app.plugins.sw_api import CAROLINE_SW_KEY, SessionExpiredError, SessionManager, _post_json

# role -> the api_path string reforce's microagent_evolution_settings/
# microagent_clones/microagent_tournaments key on -- kept identical to each
# role's own former `reply:*` command name, for admin continuity with
# whatever settings/tournament rows already exist from when these ran
# server-side.
API_PATH_BY_ROLE = {
    "turn.permission.guard": "reply:permissionGuard",
    "turn.refusal.guard": "reply:refusalGuard",
    "reply.review": "reply:review",
    "turn.action.summary": "reply:actionSummary",
    "turn.reply.expected": "reply:replyExpected",
    "turn.background.relay.worth": "reply:backgroundRelayWorth",
    "content.summarize": "reply:summarize",
    "reply.translate": "reply:translate",
}


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


_sessions = SessionManager()


async def _evolution_post(command: str, **fields: Any) -> dict[str, Any] | None:
    """One evolution: command, made with the user's SW session (2026-10-22): the
    server keeps this client's evolution per SW account -- every machine the
    account runs Caroline on shares it -- and takes the account from the session."""
    async def post(session: str) -> dict[str, Any]:
        data = await _post_json({"command": command, "key": CAROLINE_SW_KEY, "session": session, **fields}, timeout=30.0, retries=1)
        reason = str(data.get(".reason") or "")
        if data.get(".status") != "ok" and "session" in reason.lower():
            raise SessionExpiredError(reason)
        return data

    try:
        data = await _sessions.with_session(post)
    except Exception as exc:
        log_event("engine", "evolution_client_request_failed", command=command, error=str(exc))
        return None
    if data.get(".status") != "ok":
        # Unlike the reply:*/microagent:call envelopes (which also carry
        # their own inner "status"/"output"/"error"), these three commands'
        # payloads are plain data merged straight into the envelope by
        # makeResponse -- the ONLY status that exists here is the envelope's
        # own ".status". Confirmed live (2026-10-18): checking for a second,
        # non-existent inner "status" field made every resolveCalls/
        # mintClone/recordOutcome call look like a failure even once the ACL
        # was granted and the server answered "ok".
        log_event("engine", "evolution_client_bad_response", command=command, dot_status=data.get(".status"), api_reason=data.get(".reason"))
        return None
    return data


def _register(runtime: Any, agent: Any) -> None:
    try:
        runtime.register(agent)
    except ValueError:
        pass  # already registered this process lifetime -- fine, same object either way


def _run_sync(agent: Any, agent_input: dict[str, Any]) -> dict[str, Any]:
    """Synchronous -- Microagent.Runtime.run() is sync by design (see
    microagent_client.py's own module docstring); every caller below wraps
    this in asyncio.to_thread."""
    runtime = build_local_runtime()
    if runtime is None:
        raise RuntimeError("no local Microagent runtime available")
    _register(runtime, agent)
    context = runtime.new_context(budget=new_budget())
    return runtime.run(agent.name, agent_input, context)


def _run_pair_sync(champion: Any, challenger: Any, agent_input: dict[str, Any]) -> tuple[dict, dict]:
    runtime = build_local_runtime()
    if runtime is None:
        raise RuntimeError("no local Microagent runtime available")
    _register(runtime, champion)
    _register(runtime, challenger)
    context = runtime.new_context(budget=new_budget())
    results = runtime.run_many([(champion.name, agent_input), (challenger.name, agent_input)], context)
    return results[0], results[1]


async def _resolve(api_path: str, role: str, seed_version: str) -> dict[str, Any] | None:
    # The tab's current turn (2026-10-24): the server records the role against it,
    # for the user's reaction to that turn (send_reaction).
    from app import memory_turns
    from app.session_context import get_tab_id
    tab_id = get_tab_id()
    turn = memory_turns.current_turn(tab_id)
    fields = {"turnId": turn["id"]} if turn else {}
    resolved = await _evolution_post("evolution:resolveCalls", apiPath=api_path,
                                     calls=[{"role": role, "seedVersion": seed_version}], **fields)
    results = (resolved or {}).get("results")
    return results[0] if isinstance(results, list) and len(results) == 1 else None


def _ladder_summary(ladder: Any) -> str:
    """The mutator's view of a ladder -- same text as reforce's Api2Evolution._ladder_summary."""
    parts = []
    for r in ladder or M.LADDER:
        label = r.model_class + (" (with rejection reason)" if r.with_rejection_reason else "")
        if r.reasoning:
            label += f", reasoning={r.reasoning}"
        parts.append(label)
    return " -> ".join(parts)


def _new_ladder(ladder_change: str, parent_ladder: Any) -> Any:
    """A new clone's ladder from the mutator's ladder_change -- computed here, by the
    client that owns the agent (the server stores it as given); same mapping as
    reforce's own mint_clone for server roles."""
    if ladder_change == "widen_to_mini_first":
        return (M.Rung(M.MODEL_MINI, False, None), M.Rung(M.MODEL_MINI, True, None))
    if ladder_change == "narrow_to_nano_only":
        return (M.Rung(M.MODEL_NANO, False, M.REASONING_LOW), M.Rung(M.MODEL_NANO, True, M.REASONING_LOW))
    return parent_ladder


async def run_reply_agent(role: str, agent_input: dict[str, Any]) -> dict[str, Any]:
    """Runs one `reply:` domain microagent call -- plain, or as part of an
    evolution tournament -- entirely on this client. Returns the same
    {"status", "output", "error"} shape Microagent.Runtime.run() always
    returns. Fail-open at every network step: a resolve/mint/recordOutcome
    call that fails never blocks the real work, it just falls back to
    running the role's own current champion genome (or, if nothing could be
    resolved at all, the role's own plain code-defined agent) directly --
    same philosophy as every other microagent check in this codebase. Only
    raises if the LOCAL runtime itself is unavailable (Microagent.py
    couldn't be resolved) or the role is unknown -- a genuine programmer
    error, not a transient condition."""
    entry = EVOLVABLE_ROLES[role]
    default_agent = entry.parent_agent()
    api_path = API_PATH_BY_ROLE[role]

    # 2026-10-22: the server is the genome's one source of truth -- per SW account,
    # it keeps this role's evolution and always hands back the current genome. This
    # client only initializes it: the agent's version goes with every call, and the
    # server answers "seed_needed" when it has no seed yet, or one from an older
    # version (explicit re-initialization -- an edit of the text alone does not).
    seed_text, seed_ladder_json, seed_version = seed_genome(role)
    resolution = await _resolve(api_path, role, seed_version)
    if resolution is not None and resolution.get("mode") == "seed_needed":
        registered = await _evolution_post(
            "evolution:registerSeed", apiPath=api_path, role=role, rulesText=seed_text,
            ladderJson=seed_ladder_json, seedVersion=seed_version,
        )
        log_event("engine", "evolution_seed_registered", role=role, version=seed_version,
                  outcome=(registered or {}).get("outcome"))
        resolution = await _resolve(api_path, role, seed_version) if registered is not None else None

    if resolution is None or resolution.get("mode") == "seed_needed" or (resolution.get("mode") == "plain" and resolution.get("rulesText") is None):
        # No answer from the server (network, no session) -- the role's own seed,
        # from this client's code.
        log_event("engine", "evolution_genome_ran", role=role, genome="seed (local)", reason="no server answer")
        return await asyncio.to_thread(_run_sync, default_agent, agent_input)

    mode = resolution.get("mode")
    if mode == "plain":
        log_event("engine", "evolution_genome_ran", role=role, genome=resolution["agentName"])
        agent = resolve_agent_for_clone(role, resolution["agentName"], resolution["rulesText"], ladder_from_json(resolution.get("ladderJson")))
        return await asyncio.to_thread(_run_sync, agent, agent_input)

    if mode == "mutate_needed":
        champion = resolution["championClone"]
        parent_ladder = ladder_from_json(champion.get("ladderJson"))
        mutator_input = {
            "purpose": default_agent.description, "parent_rules_text": champion["rulesText"],
            "parent_ladder_summary": _ladder_summary(parent_ladder),
        }
        mutate_result = await asyncio.to_thread(_run_sync, mutator.llm_agents()[0], mutator_input)
        minted = None
        if mutate_result.get("status") == "ok":
            mutated = mutate_result["output"]
            minted = await _evolution_post(
                "evolution:mintClone", apiPath=api_path, role=role,
                mutatedRulesText=mutated["mutated_rules_text"], ladderChange=mutated["ladder_change"],
                ladderJson=ladder_to_json(_new_ladder(mutated["ladder_change"], parent_ladder)),
                rationale=mutated.get("rationale"),
            )
        if minted is None or "result" not in minted:
            # Mutation (or minting) failed -- the current genome the server just
            # handed us; never block the real call on this.
            log_event("engine", "evolution_genome_ran", role=role, genome=champion["agentName"], reason="mint failed")
            agent = resolve_agent_for_clone(role, champion["agentName"], champion["rulesText"], parent_ladder)
            return await asyncio.to_thread(_run_sync, agent, agent_input)
        resolution = minted["result"]
        mode = "tournament"
    log_event("engine", "evolution_genome_ran", role=role, genome="tournament",
              champion=resolution["champion"]["agentName"], challenger=resolution["challenger"]["agentName"])

    # mode == "tournament"
    champion_g, challenger_g = resolution["champion"], resolution["challenger"]
    champion_agent = resolve_agent_for_clone(role, champion_g["agentName"], champion_g["rulesText"], ladder_from_json(champion_g.get("ladderJson")))
    challenger_agent = resolve_agent_for_clone(role, challenger_g["agentName"], challenger_g["rulesText"], ladder_from_json(challenger_g.get("ladderJson")))

    try:
        champion_result, challenger_result = await asyncio.to_thread(_run_pair_sync, champion_agent, challenger_agent, agent_input)
    except Exception as exc:
        log_event("engine", "evolution_client_tournament_run_failed", role=role, error=str(exc))
        return await asyncio.to_thread(_run_sync, default_agent, agent_input)

    a_is_champion = random.random() < 0.5  # closes off positional/identity bias before it can start
    candidate_a = champion_result if a_is_champion else challenger_result
    candidate_b = challenger_result if a_is_champion else champion_result
    select_input = {
        "role_purpose": default_agent.description,
        "task_input_summary": _safe_json(agent_input),
        "candidate_a_output": _safe_json(candidate_a.get("output")),
        "candidate_b_output": _safe_json(candidate_b.get("output")),
    }
    # The judge's context (2026-10-24, explicit order): the champion's full rules, the
    # user's request and the last lines of the dialogue -- app/evolution_request.py.
    from app.evolution_request import current_request_context
    request = current_request_context()
    judge_context = {"role_rules": champion_g.get("rulesText"), "user_request": request.get("userRequest"),
                     "recent_dialogue": "\n".join(request.get("recentDialogue") or [])}
    select_input.update({key: value for key, value in judge_context.items() if value})
    select_result = await asyncio.to_thread(_run_sync, selector.llm_agents()[0], select_input)
    if select_result.get("status") != "ok":
        # Fail-open: an unjudgeable round still answers with SOMETHING -- the
        # champion, never an untested challenger -- and is never reported as
        # a win for anyone.
        log_event("engine", "evolution_client_selector_failed", role=role, error=select_result.get("error"))
        return champion_result

    verdict = select_result["output"]["winner"]
    if verdict == "tie":
        winner_side = "tie"
    else:
        winner_side = "champion" if (verdict == "A") == a_is_champion else "challenger"

    await _evolution_post("evolution:recordOutcome", tournamentId=resolution["tournamentId"], winner=winner_side)
    return challenger_result if winner_side == "challenger" else champion_result


async def send_reaction(turn_id: str, score: float) -> list[dict[str, Any]] | None:
    """The user's reaction to one of their turns (2026-10-24): evolution:reaction
    applies it to every evolvable role the server recorded for that turn -- the
    memory roles it ran itself and the client roles resolved with that turn id.
    Returns what the server did, role by role, or None on a failure (logged)."""
    data = await _evolution_post("evolution:reaction", turnId=turn_id, score=score)
    if data is None:
        return None
    return data.get("applied") or []

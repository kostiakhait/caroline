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

from app import evolution_journal
from app.evolvable_roles_client import EVOLVABLE_ROLES, ladder_from_json, resolve_agent_for_clone
from app.logging_setup import log_event
from app.microagent_client import build_local_runtime, new_budget
from app.microagents import mutator, selector
from app.plugins.sw_api import CAROLINE_SW_KEY, _post_json

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


async def _evolution_post(command: str, **fields: Any) -> dict[str, Any] | None:
    body = {"command": command, "key": CAROLINE_SW_KEY, **fields}
    try:
        data = await _post_json(body, timeout=30.0, retries=1)
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


async def run_reply_agent(
    role: str, agent_input: dict[str, Any], *,
    workspace_dir: str | None = None, action_id: str | None = None, track_id: str | None = None,
) -> dict[str, Any]:
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
    error, not a transient condition.

    `workspace_dir`/`action_id`/`track_id` (2026-10-19, the evolution-
    feedback loop, per explicit special order): when a call actually
    resolves through the tournament branch below (a real evolution.selector
    verdict happens), the round is journaled (app/evolution_journal.py)
    against whatever action/track it was serving -- the only way the new
    reaction-scoring/backpropagation agents can later find "the recent
    rounds for this action/track" at all, since Camerlengo itself never
    sees this correlation (see that module's own docstring). Any caller
    that omits workspace_dir simply gets no journal entry for its calls --
    never a hard requirement, same fail-open posture as everything else
    here."""
    entry = EVOLVABLE_ROLES[role]
    default_agent = entry.parent_agent()
    api_path = API_PATH_BY_ROLE[role]

    resolution: dict[str, Any] | None = None
    resolved = await _evolution_post("evolution:resolveCalls", apiPath=api_path, calls=[{"role": role}])
    if resolved is not None:
        results = resolved.get("results")
        if isinstance(results, list) and len(results) == 1:
            resolution = results[0]

    if resolution is None or (resolution.get("mode") == "plain" and resolution.get("rulesText") is None):
        # No server resolution available (network hiccup, or no
        # EvolvableRoles entry at all server-side) -- the role's own plain
        # code-defined agent, unmodified. Same zero-overhead common path as
        # today whenever nothing is evolving.
        return await asyncio.to_thread(_run_sync, default_agent, agent_input)

    mode = resolution.get("mode")
    if mode == "plain":
        agent = resolve_agent_for_clone(role, resolution["agentName"], resolution["rulesText"], ladder_from_json(resolution.get("ladderJson")))
        return await asyncio.to_thread(_run_sync, agent, agent_input)

    if mode == "mutate_needed":
        mutate_result = await asyncio.to_thread(_run_sync, mutator.llm_agents()[0], resolution["mutatorInput"])
        minted = None
        if mutate_result.get("status") == "ok":
            mutated = mutate_result["output"]
            minted = await _evolution_post(
                "evolution:mintClone", apiPath=api_path, role=role,
                mutatedRulesText=mutated["mutated_rules_text"], ladderChange=mutated["ladder_change"],
                rationale=mutated.get("rationale"),
            )
        if minted is None or "result" not in minted:
            # Mutation (or minting) failed -- fall back to the champion
            # genome resolve_calls already handed us; never block the real
            # call on this.
            champion = resolution["championClone"]
            agent = resolve_agent_for_clone(role, champion["agentName"], champion["rulesText"], ladder_from_json(champion.get("ladderJson")))
            return await asyncio.to_thread(_run_sync, agent, agent_input)
        resolution = minted["result"]
        mode = "tournament"

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
    if workspace_dir:
        evolution_journal.append(
            workspace_dir, role=role, api_path=api_path, tournament_id=resolution["tournamentId"],
            winner=winner_side, verdict_reason=select_result["output"].get("reason"),
            action_id=action_id, track_id=track_id,
        )
    return challenger_result if winner_side == "challenger" else champion_result


async def get_role_settings(role: str) -> dict[str, Any] | None:
    """evolution:getSettings for one role's own api_path -- `scope` is
    deliberately omitted (Camerlengo defaults it to this caller's own key
    identity, same as resolveCalls/mintClone/recordOutcome already do --
    2026-10-19 addition to Api2EvolutionCommands.py's getSettings/
    setSettings, so a non-admin client never has to know its own opaque
    scope string just to read/adjust its own weight_selector). Returns the
    wire-shaped settings dict, or None on any failure or unknown role."""
    api_path = API_PATH_BY_ROLE.get(role)
    if not api_path:
        return None
    data = await _evolution_post("evolution:getSettings", apiPath=api_path, role=role)
    if data is None:
        return None
    return data.get("settings")


async def apply_weight_selector_delta(role: str, delta: float) -> tuple[float, float] | None:
    """evolution.backprop's own apply step (2026-10-19, per explicit special
    order): the model only ever RECOMMENDS a bounded delta (see
    app/microagents/evolution_backprop.py's own module docstring) -- this
    function is the deterministic code that actually fetches the current
    weight_selector, clamps the result to [0.0, 1.0], and writes it back.
    Returns (weight_before, weight_after) on success, or None on any
    failure (fail-open: a skipped adjustment is never retried
    synchronously, same posture as every network call in this module)."""
    api_path = API_PATH_BY_ROLE.get(role)
    if not api_path:
        return None
    settings = await get_role_settings(role)
    before = float((settings or {}).get("weightSelector", 1.0))
    after = max(0.0, min(1.0, before + delta))
    result = await _evolution_post("evolution:setSettings", apiPath=api_path, role=role, weightSelector=after)
    if result is None:
        return None
    return before, after

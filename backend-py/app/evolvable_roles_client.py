"""Client-side mirror of reforce's apps/microagents/EvolvableRoles.py, for
the `reply:` domain only. Per explicit instruction (2026-10-18): Camerlengo
is a universal server, so a product-specific agent's own code must live and
EXECUTE on that product's own client, never on the server -- see
app/evolution_client.py's own module docstring for the full protocol this
supports.

The agents themselves live here (2026-10-22): their structural contract
(input_schema/output_schema/verify/is_empty/description) and the seed they
start from (seed_rules_text) -- reforce's EvolvableRoles.py no longer lists
them. The GENOME's one source of truth is the server: per SW account it keeps
the seed this client initialized it with (seed_genome, evolution:registerSeed
-- again only from a newer agent version), clones with their texts,
tournaments and the champion, and always hands back the current genome. This
file BUILDS a runnable M.Agent from whatever (rules_text, ladder) genome the
server hands back: every fixed field is copied verbatim from the role's own
agent module, only `build_prompt`/`ladder` ever differ.

Only the 8 `reply:` domain roles are covered here (the `memory:` domain
stays server-side permanently, per the same instruction -- it is genuinely
cross-product shared infrastructure, not migrated by this change)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.microagent_client import Microagent as M
from app.microagents import (
    action_summary,
    background_relay_worth,
    content_summary,
    permission_guard,
    refusal_guard,
    reply_expected,
    reply_review,
    reply_translate,
)


@dataclass(frozen=True)
class EvolvableRole:
    """parent_agent () -> the current code-defined M.Agent for this role,
    called fresh every time (never cached) so a later edit to the role's own
    vendored module is picked up without touching this file.

    render (rules_text, input) -> the clone's full prompt -- same shape and
    same per-role substitutions as reforce's EvolvableRoles.py (kept
    byte-identical on purpose, since a clone's genome/ladder may itself have
    come FROM the server, which rendered it with the exact same rules)."""

    parent_agent: Callable[[], Any]
    render: Callable[[str, Any], str]
    # The role's seed rules text, read fresh like parent_agent -- registered on the
    # server as this role's seed genome (see seed_genome below).
    seed_rules_text: Callable[[], str] = lambda: ""


EVOLVABLE_ROLES: dict[str, EvolvableRole] = {
    permission_guard.GUARD: EvolvableRole(
        parent_agent=lambda: permission_guard.llm_agents()[0],
        seed_rules_text=lambda: permission_guard.GUARD_RULES_TEXT,
        render=lambda rules_text, input: rules_text + permission_guard._data_section(input),
    ),
    refusal_guard.GUARD: EvolvableRole(
        parent_agent=lambda: refusal_guard.llm_agents()[0],
        seed_rules_text=lambda: refusal_guard.GUARD_RULES_TEXT,
        render=lambda rules_text, input: rules_text + refusal_guard._data_section(input),
    ),
    reply_review.REVIEW: EvolvableRole(
        parent_agent=lambda: reply_review.llm_agents()[0],
        seed_rules_text=lambda: reply_review.REVIEW_RULES_TEXT,
        render=lambda rules_text, input: rules_text + reply_review._data_section(input),
    ),
    action_summary.SUMMARY: EvolvableRole(
        parent_agent=lambda: action_summary.llm_agents()[0],
        seed_rules_text=lambda: action_summary.SUMMARY_RULES_TEXT,
        render=lambda rules_text, input: rules_text + action_summary._data_section(input),
    ),
    reply_expected.EXPECTED: EvolvableRole(
        parent_agent=lambda: reply_expected.llm_agents()[0],
        seed_rules_text=lambda: reply_expected.EXPECTED_RULES_TEXT,
        render=lambda rules_text, input: rules_text + reply_expected._data_section(input),
    ),
    background_relay_worth.WORTH: EvolvableRole(
        parent_agent=lambda: background_relay_worth.llm_agents()[0],
        seed_rules_text=lambda: background_relay_worth.WORTH_RULES_TEXT,
        render=lambda rules_text, input: (
            rules_text.replace("{{TASK_CLAUSE}}", background_relay_worth._task_clause(input))
            + background_relay_worth._data_section(input)
        ),
    ),
    content_summary.SUMMARIZE: EvolvableRole(
        parent_agent=lambda: content_summary.llm_agents()[0],
        seed_rules_text=lambda: content_summary.SUMMARIZE_RULES_TEXT,
        render=lambda rules_text, input: (
            rules_text.replace("{{TARGET_CHARS}}", str(int(input.get("target_chars") or content_summary.DEFAULT_TARGET_CHARS)))
            + content_summary._data_section(input)
        ),
    ),
    reply_translate.TRANSLATE: EvolvableRole(
        parent_agent=lambda: reply_translate.llm_agents()[0],
        seed_rules_text=lambda: reply_translate.TRANSLATE_RULES_TEXT,
        render=lambda rules_text, input: (
            rules_text.replace("{{LANGUAGE}}", input["language"]) + reply_translate._data_section(input)
        ),
    ),
}


def ladder_to_json(ladder: Optional[tuple]) -> Optional[str]:
    if not ladder:
        return None
    return json.dumps([
        {"model_class": r.model_class, "with_rejection_reason": r.with_rejection_reason, "reasoning": r.reasoning}
        for r in ladder
    ])


def ladder_from_json(raw: Optional[str]) -> Optional[tuple]:
    if not raw:
        return None
    return tuple(
        M.Rung(r["model_class"], r["with_rejection_reason"], r.get("reasoning"))
        for r in json.loads(raw)
    )


def resolve_agent_for_clone(role: str, clone_name: str, rules_text: str, ladder: Optional[tuple]) -> Any:
    """Builds the real M.Agent for one genome (the role's own current
    champion, or a clone) the server handed back. Every fixed field is
    copied verbatim -- by reference -- from the role's own vendored parent
    agent; only `name`, `build_prompt` (a closure over this genome's own
    rules_text) and `ladder` ever differ. Mirrors reforce's
    EvolvableRoles.resolve_agent_for_clone exactly."""
    entry = EVOLVABLE_ROLES[role]
    parent = entry.parent_agent()
    assert parent.execute is None and parent.build_prompt is not None, (
        f"resolve_agent_for_clone only supports build_prompt agents -- {role!r}'s parent_agent is a "
        "composite (execute=) agent, which this client does not run yet"
    )
    render = entry.render

    def build_prompt(input: Any, context: Any) -> str:
        return render(rules_text, input)

    return M.Agent(
        name=clone_name,
        version=parent.version,
        description=parent.description,
        input_schema=parent.input_schema,
        output_schema=parent.output_schema,
        build_prompt=build_prompt,
        verify=parent.verify,
        is_empty=parent.is_empty,
        execute=None,
        images=parent.images,
        ladder=ladder,
        deadline_s=parent.deadline_s,
    )


def seed_genome(role: str) -> tuple[str, Optional[str], str]:
    """(rules text, ladder JSON, agent version) of the role's seed as this client's
    code has it (2026-10-22). The server is the genome's one source of truth: the
    client initializes it with this seed when the server has none, and re-initializes
    it only from a NEWER agent version -- an edit of the text alone never reaches the
    server; bump the agent's version to send it."""
    entry = EVOLVABLE_ROLES[role]
    parent = entry.parent_agent()
    return entry.seed_rules_text(), ladder_to_json(parent.ladder), str(parent.version)

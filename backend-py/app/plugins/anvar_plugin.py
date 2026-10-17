"""anvar -- Caroline's own access to Anvar, Camerlengo's email-agent manager
(reforce-microagents: apps/Anvar/main.py). An Anvar "agent" is a persistent
virtual email persona (its own mailbox, its own identity/instructions) that
writes cold outreach, replies to its own inbox on its own, and follows up
with reminders -- entirely separate from Caroline's OWN identity and from
owner-impersonating sends (email_plugin.py's email_send, voice_api.py's
owner-account checks), which this never touches: every Anvar send goes out
under THAT agent's own name/mailbox, never the owner's or Caroline's own.

Routed through the generic plugin:getAttributes/plugin:call v2 commands
(Plugins.py, API/Api2PluginCommands.py) with plugin="Anvar" -- the same
query-string interface Anvar's own module docstring documents (listAgents,
getAgent:<email>, sendFirstEmail:<json>, ...). No new server-side surface
was needed; this file is purely the Caroline-side tool wrapper, one
PluginTool per capability for the model's own clarity rather than a single
generic "query" passthrough.

Per explicit instruction (2026-10-16): Anvar was disabled after it kept
resending emails -- fixed server-side (apps/Anvar/main.py: three functions
that silently swallowed disk-write failures, erasing the persistent
duplicate-send guards; see that commit for the full incident) before being
handed to Caroline here."""

from __future__ import annotations

import json
from typing import Any

from app.context_cap import truncate
from app.login_api import get_cached_v2_session
from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.plugins.sw_api import CAROLINE_SW_KEY, _post_json
from app.sw_gate import require_sw_or_prompt
from app.session_context import get_send


async def _gate() -> dict[str, Any] | None:
    gate = await require_sw_or_prompt(get_send())
    return None if gate.ok else {"text": gate.message, "is_error": True}


async def _anvar(query: str, timeout: float = 30.0) -> tuple[Any, str | None]:
    """(result, None) or (None, error-text) -- the raw Anvar query-string
    protocol, see this module's own docstring. Never raises: every error
    path (transport, envelope, Anvar's own {"error": ...} shape) collapses
    to the second tuple slot so every tool function below has one place to
    turn it into a model-facing message."""
    body: dict[str, Any] = {"command": "plugin:getAttributes", "key": CAROLINE_SW_KEY, "plugin": "Anvar", "query": query}
    session = await get_cached_v2_session()
    if session:
        body["session"] = session
    try:
        data = await _post_json(body, timeout=timeout)
    except Exception as exc:
        log_event("plugin:anvar", "request_failed", query=query, error=str(exc))
        return None, f"Anvar request failed: {exc}"
    if data.get(".status") != "ok":
        return None, data.get(".reason") or f"Anvar request failed (status {data.get('.status')})"
    result = data.get("result")
    if isinstance(result, dict) and result.get("error"):
        return None, str(result["error"])
    return result, None


def _ok(result: Any) -> dict[str, Any]:
    # Bug fix (2026-10-16), confirmed live (full-plugin context-overflow audit):
    # anvar_list_agents/operations/threads/recipients can grow unboundedly over an
    # agent's lifetime with no cap anywhere -- truncated here, the one chokepoint
    # every anvar_* tool's result passes through, a no-op for every small result.
    text = json.dumps(result, ensure_ascii=False)
    window, note = truncate(text)
    if note is None:
        return {"text": text}
    return {"text": window + "\n\n[... result truncated: too large to return in full; narrow with a more specific filter/id ...]"}


def _err(message: str) -> dict[str, Any]:
    return {"text": message, "is_error": True}


async def anvar_list_agents(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    result, error = await _anvar("listAgents")
    return _err(error) if error else _ok(result)


async def anvar_get_agent(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    email = (args.get("email") or "").strip()
    if not email:
        return _err('anvar_get_agent needs "email".')
    result, error = await _anvar(f"getAgent:{email}")
    return _err(error) if error else _ok(result)


async def anvar_set_agent(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    agent = args.get("agent")
    if not isinstance(agent, dict):
        return _err('anvar_set_agent needs "agent": an object with the agent\'s fields.')
    result, error = await _anvar("setAgent:" + json.dumps(agent, ensure_ascii=False))
    return _err(error) if error else _ok(result)


async def anvar_set_field(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    email = (args.get("email") or "").strip()
    field = (args.get("field") or "").strip()
    value = args.get("value")
    if not email or not field or value is None:
        return _err('anvar_set_field needs "email", "field" and "value".')
    result, error = await _anvar(f"setField:{email}:{field}:{value}")
    return _err(error) if error else _ok(result)


async def anvar_delete_agent(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    email = (args.get("email") or "").strip()
    if not email:
        return _err('anvar_delete_agent needs "email".')
    result, error = await _anvar(f"deleteAgent:{email}")
    return _err(error) if error else _ok(result)


async def anvar_enable_agent(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    email = (args.get("email") or "").strip()
    if not email:
        return _err('anvar_enable_agent needs "email".')
    result, error = await _anvar(f"enableAgent:{email}")
    return _err(error) if error else _ok(result)


async def anvar_disable_agent(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    email = (args.get("email") or "").strip()
    if not email:
        return _err('anvar_disable_agent needs "email".')
    result, error = await _anvar(f"disableAgent:{email}")
    return _err(error) if error else _ok(result)


async def anvar_send_first_email(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    agent = (args.get("agent") or "").strip()
    to = (args.get("to") or "").strip()
    if not agent or not to:
        return _err('anvar_send_first_email needs "agent" (the sending agent\'s email) and "to".')
    payload = {"agent": agent, "to": to}
    for key in ("to_name", "to_description", "language"):
        if args.get(key):
            payload[key] = args[key]
    result, error = await _anvar("sendFirstEmail:" + json.dumps(payload, ensure_ascii=False))
    return _err(error) if error else _ok(result)


async def anvar_get_operation_status(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    uid = (args.get("uid") or "").strip()
    if not uid:
        return _err('anvar_get_operation_status needs "uid" (from anvar_send_first_email\'s own reply).')
    result, error = await _anvar(f"getOperationStatus:{uid}")
    return _err(error) if error else _ok(result)


async def anvar_list_operations(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    result, error = await _anvar("listOperations")
    return _err(error) if error else _ok(result)


async def anvar_agent_status(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    email = (args.get("email") or "").strip()
    if not email:
        return _err('anvar_agent_status needs "email".')
    result, error = await _anvar(f"agentStatus:{email}")
    return _err(error) if error else _ok(result)


async def anvar_list_threads(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    email = (args.get("email") or "").strip()
    if not email:
        return _err('anvar_list_threads needs "email" (the agent\'s own address).')
    result, error = await _anvar(f"listThreads:{email}")
    return _err(error) if error else _ok(result)


async def anvar_list_recipients(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    agent = (args.get("agent") or "").strip()
    if not agent:
        return _err('anvar_list_recipients needs "agent" (the agent\'s own address).')
    result, error = await _anvar(f"listRecipients:{agent}")
    return _err(error) if error else _ok(result)


async def anvar_delete_recipient(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    agent = (args.get("agent") or "").strip()
    recipient = (args.get("recipient") or "").strip()
    if not agent or not recipient:
        return _err('anvar_delete_recipient needs "agent" and "recipient".')
    result, error = await _anvar(f"deleteRecipient:{agent}:{recipient}")
    return _err(error) if error else _ok(result)


async def anvar_pause_recipient(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    agent = (args.get("agent") or "").strip()
    recipient = (args.get("recipient") or "").strip()
    if not agent or not recipient:
        return _err('anvar_pause_recipient needs "agent" and "recipient".')
    result, error = await _anvar(f"pauseRecipient:{agent}:{recipient}")
    return _err(error) if error else _ok(result)


async def anvar_resume_recipient(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    agent = (args.get("agent") or "").strip()
    recipient = (args.get("recipient") or "").strip()
    if not agent or not recipient:
        return _err('anvar_resume_recipient needs "agent" and "recipient".')
    result, error = await _anvar(f"resumeRecipient:{agent}:{recipient}")
    return _err(error) if error else _ok(result)


_USAGE_INSTRUCTIONS = (
    "Anvar manages persistent virtual email personas (\"agents\"), each with its own real mailbox, its own "
    "identity/instructions, and its own INBOX -- it reads its own mail, replies on its own, and follows up "
    "with reminders, all under ITS OWN name, never yours and never Caroline's own identity. Use this for "
    "cold outreach or an ongoing correspondence that genuinely belongs to a persona (a recruiting contact, a "
    "support alias, a research assistant persona) -- never as a way to send something under the owner's own "
    "name (that is email_plugin.py's email_send, with its own approval rules) or under Caroline's own "
    "identity.\n\n"
    "An agent's fields: name, email (key, fixed once created), password, server, owner_email, "
    "general_instruction (who it is), first_email_instruction (how to open a new correspondence), "
    "reply_instruction (how to answer), enabled, reminder_interval_days (0 = off), reminder_max_count "
    "(0 = unlimited). anvar_set_agent creates or wholesale-replaces one; anvar_set_field changes a single "
    "field of an existing one.\n\n"
    "anvar_send_first_email queues a cold-outreach email from an existing, enabled agent and returns at once "
    "with a \"uid\" -- the actual send happens in the background (composing + SMTP can take a few seconds); "
    "poll anvar_get_operation_status(uid) if you need to know it actually went out. Already-sent/in-progress "
    "sends to the same recipient are silently skipped server-side (persistent guard, survives a restart) -- "
    "you do not need to check first yourself.\n\n"
    "anvar_list_recipients/anvar_list_threads show who an agent has written to and the reminder/follow-up "
    "state of each; anvar_pause_recipient stops reminders (and incoming auto-replies) to one address without "
    "deleting the history, anvar_resume_recipient undoes that, anvar_delete_recipient removes the record "
    "entirely."
)


PLUGIN = Plugin(
    name="anvar",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool("anvar_list_agents", "Lists every Anvar email-agent persona with a short summary (name, email, enabled, owner, connection error if any).", {}, anvar_list_agents),
        PluginTool("anvar_get_agent", "Returns every field of one Anvar agent (including its own mailbox password -- this is the agent's own account, the owner's infrastructure, not a third party's secret).", {"email": str}, anvar_get_agent),
        PluginTool(
            "anvar_set_agent",
            "Creates a new Anvar agent, or wholesale-replaces an existing one with the same email. \"agent\" needs: "
            "name, email, password, owner_email, general_instruction, first_email_instruction, reply_instruction "
            "(all required); server, enabled, reminder_interval_days, reminder_max_count are optional.",
            {"agent": dict}, anvar_set_agent,
        ),
        PluginTool("anvar_set_field", "Changes ONE field of an existing agent (email itself cannot be changed). Fields: name/password/server/owner_email/general_instruction/first_email_instruction/reply_instruction/enabled/reminder_interval_days/reminder_max_count.", {"email": str, "field": str, "value": str}, anvar_set_field),
        PluginTool("anvar_delete_agent", "Permanently deletes an Anvar agent and stops its inbox monitor.", {"email": str}, anvar_delete_agent),
        PluginTool("anvar_enable_agent", "Enables an agent: starts its inbox monitor (auto-replies, reminders) and allows sendFirstEmail for it.", {"email": str}, anvar_enable_agent),
        PluginTool("anvar_disable_agent", "Disables an agent: stops its inbox monitor and refuses new sendFirstEmail calls for it.", {"email": str}, anvar_disable_agent),
        PluginTool(
            "anvar_send_first_email",
            "Has an existing, enabled Anvar agent compose and send a first/cold-outreach email to someone, under "
            "that agent's own identity. Returns at once with a \"uid\" to check with anvar_get_operation_status; "
            "the actual send happens in the background. Already-sent/in-flight sends to the same recipient are "
            "silently skipped.",
            {"agent": str, "to": str, "to_name": str | None, "to_description": str | None, "language": str | None},
            anvar_send_first_email,
        ),
        PluginTool("anvar_get_operation_status", "Status of one anvar_send_first_email call by its \"uid\": queued | running | done | error.", {"uid": str}, anvar_get_operation_status),
        PluginTool("anvar_list_operations", "Lists every anvar_send_first_email operation and its status.", {}, anvar_list_operations),
        PluginTool("anvar_agent_status", "Connection/health status of one agent's inbox monitor.", {"email": str}, anvar_agent_status),
        PluginTool("anvar_list_threads", "An agent's reminder/follow-up queue: every recipient it has an open thread with, reminder counts, and when the next reminder is due.", {"email": str}, anvar_list_threads),
        PluginTool("anvar_list_recipients", "Everyone an agent has ever contacted, with first/last contact time and whether they're paused.", {"agent": str}, anvar_list_recipients),
        PluginTool("anvar_delete_recipient", "Removes a recipient's whole record (thread + reminder history) for one agent.", {"agent": str, "recipient": str}, anvar_delete_recipient),
        PluginTool("anvar_pause_recipient", "Pauses an agent's reminders and auto-replies to one recipient, without deleting the history.", {"agent": str, "recipient": str}, anvar_pause_recipient),
        PluginTool("anvar_resume_recipient", "Undoes anvar_pause_recipient for one recipient.", {"agent": str, "recipient": str}, anvar_resume_recipient),
    ],
)

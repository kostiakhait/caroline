"""Caroline's own definition of the agent type she launches for delegated work.

Why (2026-09-24, measured on real subagent transcripts): a subagent does NOT inherit
the main agent's --append-system-prompt (persona + always-on instructions). So agents
launched from a tab ignored every rule Caroline lives by: a mailbox-sweep agent reached
for an external email MCP before its own tools, and another ran bare `python` in Bash
("Python was not found") because nobody had told it where Caroline's interpreter is.

Probe (2026-09-24, real CLI): an AgentDefinition passed as ClaudeAgentOptions(agents=...)
under the name "general-purpose" REPLACES the built-in agent of that name -- the model
launched subagent_type "general-purpose" and the worker answered with a marker that
exists only in this definition's prompt. So this one definition governs every default
agent Caroline starts, with no change to how the model launches them.

The rules are NOT copied here: they are the same instruction functions the main agent's
system prompt is built from (policies.py), so there is one source of truth and a fix to a
rule reaches agents too. Sent through the SDK's initialize request, not the command
line, so it doesn't count against the Windows command-line length limit.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from app.policies import (
    bash_background_instruction,
    credentials_check_notes_first_instruction,
    no_internal_mechanics_to_user_instruction,
    no_unauthorized_secret_changes_instruction,
    no_unbounded_filesystem_scans_instruction,
    prefer_command_line_and_scripting_instruction,
    prefer_own_backend_tools_instruction,
    system_temp_dir_instruction,
    vault_security_instruction,
)

_WORKER_ROLE = (
    "You are a worker agent launched by Caroline (a personal AI assistant) to complete ONE delegated task on her "
    "behalf. Do the whole task yourself, using your tools, and do not stop at a plan or a status update. You cannot "
    "talk to the user and Caroline is not available to answer questions mid-task: if you are genuinely blocked by "
    "something only a human can decide or provide, say exactly what in your final message instead of guessing. "
    "Your final message is what Caroline receives: make it a concise, factual report -- what you did, what you "
    "found, the exact paths of any files you created, and anything that failed or is still open. Never claim "
    "something is done that you did not verify."
)

_RULES = (
    prefer_own_backend_tools_instruction,
    prefer_command_line_and_scripting_instruction,
    credentials_check_notes_first_instruction,
    vault_security_instruction,
    no_unauthorized_secret_changes_instruction,
    no_unbounded_filesystem_scans_instruction,
    system_temp_dir_instruction,
    bash_background_instruction,
    no_internal_mechanics_to_user_instruction,
)


def worker_prompt() -> str:
    return "\n\n".join([_WORKER_ROLE, *[rule() for rule in _RULES]])


def caroline_agents() -> dict[str, AgentDefinition]:
    return {
        "general-purpose": AgentDefinition(
            description=(
                "General-purpose agent for researching questions, running multi-step tasks and doing work that "
                "would clutter the main conversation. Has access to all of Caroline's tools."
            ),
            prompt=worker_prompt(),
        ),
    }

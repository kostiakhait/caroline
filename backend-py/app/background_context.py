"""What a background activity needs to know about the conversation it works for (2026-10-26, explicit instruction).

A finished sub-agent or background operation is handled in its own fresh, invisible session (app/pseudo_tab.py: one thread of
reasoning per activity, a deliberate decision). That session knows nothing of the user's conversation: on 2026-10-26 the one that
received "your sub-agent finished" found its dialogue file empty, concluded the user had never asked for the task (rejecting
pending GitLab requests, asked for in plain words two minutes earlier) and asked the user to confirm it again -- in Caroline's
voice. The isolation stays; this block hands the session the facts it lacks, from the exact record of the owning tab: the last
messages as the user saw them, what is waiting for their decision, and what is already done."""

from __future__ import annotations

from typing import Any

from app.logging_setup import log_event

DIALOGUE_MESSAGES = 12
MAX_CHARS = 9000

GUIDANCE = (
    "The user's own words are in the record above: if the task this result belongs to was asked for there, it was authorized by "
    "them -- never ask them to confirm it again and never say you have no instruction for it. Tell the user about the result "
    "ONLY if what it shows has not been told to them already (see the record); if it has, reply exactly [[NO_UPDATE]]."
)


def context_block(workspace_dir: str, real_tab_id: str, sessions_getter: Any = None) -> str:
    """The block for a background activity of `real_tab_id`; '' when there is nothing to tell. Never raises."""
    parts: list[str] = []
    try:
        from app import action_ledger, dialogue_log

        entries = dialogue_log.select(dialogue_log.read_entries(workspace_dir, real_tab_id), limit=DIALOGUE_MESSAGES,
                                      include_withheld=False)
        text = dialogue_log.render(entries)
        if text != "(no messages match)":
            parts.append("THE USER'S CONVERSATION, the last messages exactly as the user saw them (oldest first):\n" + text)
        done = action_ledger.checker_note(workspace_dir)
        if done:
            parts.append(done)
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "background_context_failed", tab_id=real_tab_id, error=str(exc)[:200])
    try:
        session = sessions_getter(real_tab_id) if sessions_getter is not None else None
        if session is not None:
            open_actions = [a.summary for a in session.action_registry.open_actions()]
            if open_actions:
                parts.append("WAITING FOR THE USER'S DECISION OR APPROVAL:\n" + "\n".join(f"- {s}" for s in open_actions))
    except Exception:  # noqa: BLE001
        pass
    if not parts:
        return ""
    body = "\n\n".join(parts)
    body = body[-(MAX_CHARS - len(GUIDANCE) - 2):]        # when too long the OLD end goes, the guidance always stays
    return body + "\n\n" + GUIDANCE

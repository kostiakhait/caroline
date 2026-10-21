"""The rule that decides whether a send-type tool call may run -- plain code,
no model.

Per explicit instruction (2026-10-20), after a real incident: the permission
guard (an LLM judging policy from the dialogue) blocked a Telegram file
Caroline was sending from her OWN account -- "the exact caption has not been
approved" -- and blocked it again after the user's explicit "Разрешаю". The
user's words: "Нам нужно полу-механический контроль, т.е. ЛЛМ проверяет тип
операции и от чьего имени, а алгоритм (!) разрешает или блокирует." So the
model only establishes the facts (turn.permission.identify: what the action
is, whose identity it goes out under, whether the user directly ordered it),
and this table decides:

    nothing to gate (not a communication, not money)        allowed
    communication under the assistant's own identity        allowed
    communication under a persona (crafted sender, Anvar)   allowed  (the user's choice, 2026-10-20)
    communication under the user's own identity             allowed only if the user directly ordered
    money                                                   this action or approved it; blocked otherwise

"Approved" is the open action's own flag, set by the sanction agent from the
user's real messages (app/action_registry.py). The user's own order is the
identify step's explicitly_instructed_by_user fact. This replaces, for sends,
the 2026-10-16 rule that also required the exact wording of an own-identity
send to have been shown and accepted: per the same day's instruction, an
explicit order is enough on its own.

A communication whose identity could not be classified ("n/a") is treated as
the user's own -- the side where a wrong guess costs nothing but a question.
"""

from __future__ import annotations

from typing import Any, Optional

ASSISTANT_OWN = "assistant_own"
PERSONA = "persona"
COMMUNICATION = "communication"
MONEY = "money"
NONE = "none"


def send_block_reason(facts: Optional[dict], approved: bool) -> Optional[str]:
    """None when the call may run; otherwise one sentence for the model,
    saying what needs the user's word. `facts` are turn.permission.identify's;
    `approved` is whether the open action this call belongs to is approved."""
    action_type = (facts or {}).get("action_type") or NONE
    if action_type == NONE:
        return None
    identity = facts.get("sender_identity")
    if action_type == COMMUNICATION and identity in (ASSISTANT_OWN, PERSONA):
        return None
    if facts.get("explicitly_instructed_by_user") or approved:
        return None
    summary = (facts.get("action_summary") or "this action").strip().rstrip(".")
    if action_type == MONEY:
        return f"{summary}: it affects the user's money, and the user has neither ordered nor approved it."
    return f"{summary}: it goes out under the user's own identity, and the user has neither ordered nor approved it."

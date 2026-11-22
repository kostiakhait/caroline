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
    communication under the user's own identity             allowed only after the FINAL text was shown to the
                                                            user and he said yes after it (app/own_identity_gate.py);
                                                            an order or an earlier approval is NOT enough (2026-11-20)
    money                                                   allowed only if the user directly ordered this action
                                                            or approved it; blocked otherwise

"Approved" is the open action's own flag, set by the sanction agent from the
user's real messages (app/action_registry.py). The user's own order is the
identify step's explicitly_instructed_by_user fact. For a communication under
the user's own identity this was overturned on 2026-11-20 ("Я хочу видеть финальный текст всегда"): an
order is not enough, nor is an edit he dictated; the final text is shown and answered yes, every time.

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


def is_own_identity_communication(facts: Optional[dict]) -> bool:
    """A communication that goes out under the user's own name (an unclassified identity counts as his)."""
    facts = facts or {}
    return (facts.get("action_type") or NONE) == COMMUNICATION and facts.get("sender_identity") not in (ASSISTANT_OWN, PERSONA)


def send_block_reason(
    facts: Optional[dict], approved: bool, final_text_reason: Optional[str] = None, final_text_checked: bool = False,
) -> Optional[str]:
    """None when the call may run; otherwise one sentence for the model,
    saying what needs the user's word. `facts` are turn.permission.identify's;
    `approved` is whether the open action this call belongs to is approved.
    `final_text_reason` / `final_text_checked`: the result of the final-text gate
    (app/own_identity_gate.py), which decides alone for own-identity communication;
    unchecked means blocked."""
    action_type = (facts or {}).get("action_type") or NONE
    if action_type == NONE:
        return None
    identity = facts.get("sender_identity")
    if action_type == COMMUNICATION and identity in (ASSISTANT_OWN, PERSONA):
        return None
    summary = (facts.get("action_summary") or "this action").strip().rstrip(".")
    if is_own_identity_communication(facts):
        summary = (facts.get("action_summary") or "this message").strip().rstrip(".")
        if not final_text_checked:
            return f"{summary}: it goes out under the user's own identity and its final text could not be checked against what the user was shown."
        return f"{summary}: {final_text_reason}" if final_text_reason else None
    if facts.get("explicitly_instructed_by_user") or approved:
        return None
    if action_type == MONEY:
        return f"{summary}: it affects the user's money, and the user has neither ordered nor approved it."
    return f"{summary}: it goes out under the user's own identity, and the user has neither ordered nor approved it."

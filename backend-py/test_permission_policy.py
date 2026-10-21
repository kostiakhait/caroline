"""The send rule (app/permission_policy.py): the model establishes the facts,
this table decides. Run: python -m pytest test_permission_policy.py -q"""

import pytest

from app.permission_policy import send_block_reason


def facts(action_type="communication", identity="user_own", ordered=False, summary="Send the file to Olga"):
    return {"action_type": action_type, "sender_identity": identity, "explicitly_instructed_by_user": ordered,
            "action_summary": summary}


@pytest.mark.parametrize("given", [None, {}, facts(action_type="none")])
def test_nothing_to_gate_is_allowed(given):
    assert send_block_reason(given, approved=False) is None


@pytest.mark.parametrize("identity", ["assistant_own", "persona"])
def test_the_assistants_own_account_and_a_persona_are_always_allowed(identity):
    assert send_block_reason(facts(identity=identity), approved=False) is None


def test_the_incident_of_2026_10_20_a_send_from_carolines_own_telegram_needs_nothing():
    # "Caroline is about to send the file ... from her own Telegram account", caption never approved
    assert send_block_reason(facts(identity="assistant_own", ordered=False), approved=False) is None


@pytest.mark.parametrize("identity", ["user_own", "n/a", None])
def test_a_send_as_the_user_needs_an_order_or_an_approval(identity):
    reason = send_block_reason(facts(identity=identity), approved=False)
    assert reason and "user's own identity" in reason and "Send the file to Olga" in reason
    assert send_block_reason(facts(identity=identity, ordered=True), approved=False) is None
    assert send_block_reason(facts(identity=identity), approved=True) is None


def test_money_needs_an_order_or_an_approval_whoever_pays():
    reason = send_block_reason(facts(action_type="money", identity="n/a", summary="Pay the invoice"), approved=False)
    assert reason and "money" in reason
    assert send_block_reason(facts(action_type="money", identity="assistant_own"), approved=False)
    assert send_block_reason(facts(action_type="money", ordered=True), approved=False) is None
    assert send_block_reason(facts(action_type="money"), approved=True) is None

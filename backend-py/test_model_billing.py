"""Tripwire for the 2026-10-29 wallet billing of sw model calls (app/model_billing.py): every call made through
the proxy adapter is refused while the PIA wallet is empty and charged right after it returns (real cost x 1.6,
allow_negative, no mail); an sw turn with an empty wallet does not start.

Run directly: python test_model_billing.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import model_billing as mb
from app import small_model_engine as sme

_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    if not condition:
        _FAILURES.append(name)
        print(f"FAIL: {name}")
    else:
        print(f"ok:   {name}")


class FakeServer:
    """wallet:getBalance answers `balance`; wallet:charge subtracts the amount and answers the new balance."""

    def __init__(self, balance: float) -> None:
        self.balance = balance
        self.calls: list[dict] = []
        self.fail_reads = False

    def __call__(self, body: dict) -> dict:
        self.calls.append(body)
        if body["command"] == "wallet:getBalance":
            if self.fail_reads:
                raise RuntimeError("network down")
            return {".status": "ok", "balances": {"PIA": self.balance}}
        if body["command"] == "wallet:charge":
            self.balance = round(self.balance - body["amount"], 2)
            return {".status": "ok", "result": True, "balance_major": self.balance}
        raise AssertionError(body["command"])

    def commands(self) -> list[str]:
        return [c["command"] for c in self.calls]


class FakeInner:
    def __init__(self, cost: float) -> None:
        self.cost = cost
        self.models: list[str] = []

    def _resolve(self, model, messages, **kwargs):
        self.models.append(model)
        return NS(id="gen-1", usage=NS(cost=self.cost), choices=[])


def with_server(balance: float) -> FakeServer:
    server = FakeServer(balance)
    mb._post = server
    mb.mark_stale()
    return server


# --- a charge: real cost x 1.6 in PIA, debt allowed, no mail --------------------------------------------------------
server = with_server(100.0)
adapter = mb.ChargingAdapter(FakeInner(cost=0.01), "carolinekey.session-xyz")
adapter._resolve(model="anthropic/claude-sonnet-5", messages=[])
charge = [c for c in server.calls if c["command"] == "wallet:charge"][0]
check("the session is the token's second half", charge["session"] == "session-xyz")
check("amount is cost x 1.6 x 1000 PIA", charge["amount"] == 16.0)
check("debt allowed and no mail", charge["allow_negative"] is True and charge["notify"] is False)
check("item names the model", charge["item_id"] == "anthropic/claude-sonnet-5" and charge["currency"] == "PIA")
check("idempotency key from the generation id", charge["idempotency_key"] == "caroline-model-call:gen-1")
check("the balance comes back from the charge", mb._known_balance == 84.0)

# --- between turns the charge's balance is trusted, no extra read ---------------------------------------------------
server.calls.clear()
adapter._resolve(model="openai/gpt-5-nano", messages=[])
check("no balance read while it is known positive", server.commands() == ["wallet:charge"])

# --- tiny costs are not rounded away ----------------------------------------------------------------------------------
server = with_server(100.0)
mb.ChargingAdapter(FakeInner(cost=0.000002), "k.s")._resolve(model="openai/gpt-5-nano", messages=[])
check("a tiny cost is charged at least 0.01 PIA", [c["amount"] for c in server.calls if c["command"] == "wallet:charge"] == [0.01])

# --- no cost, no charge -----------------------------------------------------------------------------------------------
server = with_server(100.0)
mb.ChargingAdapter(FakeInner(cost=0), "k.s")._resolve(model="m", messages=[])
check("a free call charges nothing", "wallet:charge" not in server.commands())

# --- an empty wallet refuses the call before it is made ---------------------------------------------------------------
server = with_server(0.0)
inner = FakeInner(cost=0.01)
try:
    mb.ChargingAdapter(inner, "k.s")._resolve(model="m", messages=[])
    refused = False
except RuntimeError as exc:
    refused = "402 Insufficient funds" in str(exc)
check("empty wallet: refused with the 402 words the engine waits on", refused)
check("empty wallet: the model is not called", inner.models == [])

# --- a call that takes the wallet below zero is charged, the next one is refused ---------------------------------------
server = with_server(10.0)
inner = FakeInner(cost=0.01)
adapter = mb.ChargingAdapter(inner, "k.s")
adapter._resolve(model="m", messages=[])
check("the last call is charged into debt", server.balance == -6.0)
try:
    adapter._resolve(model="m", messages=[])
    refused = False
except RuntimeError:
    refused = True
check("the call after the debt is refused", refused and len(inner.models) == 1)

# --- a top-up is seen at once: an empty wallet is re-read on every call -----------------------------------------------
server.balance = 500.0
adapter._resolve(model="m", messages=[])
check("after a top-up the call goes through", len(inner.models) == 2)

# --- a failed balance read does not block a paying user ----------------------------------------------------------------
server = with_server(100.0)
server.fail_reads = True
inner = FakeInner(cost=0.01)
mb.ChargingAdapter(inner, "k.s")._resolve(model="m", messages=[])
check("balance read failure: the call still runs", inner.models == ["m"])

# --- an sw turn with an empty wallet does not start ------------------------------------------------------------------
if sme.camerlengo_ai is not None and not sme.MODEL_CALLS_VIA_SQUIRRELWISDOM:
    server = with_server(0.0)

    async def fake_key():
        return "k.s"

    sme.get_model_provider_key = fake_key
    result = asyncio.run(sme.run_small_model_turn("tab", ".", None, "hello", [], "en", None))
    check("empty wallet: the turn answers that the balance ran out", "balance has run out" in result.get("text", ""))
    check("empty wallet: only the balance was read", server.commands() == ["wallet:getBalance"])
else:
    print("skip: Camerlengo not importable here")

if _FAILURES:
    print(f"\n{len(_FAILURES)} FAILED")
    sys.exit(1)
print("\nall passed")

"""Charges the user's SquirrelWisdom wallet for every model call Caroline makes.

Per explicit instruction (2026-10-29): the model calls of the sw engine go through
Camerlengo's /v1/chat/completions proxy, which only tags and logs them -- it never
checks or charges a wallet, and ai:resolveAgenticStep (the billed route) is still
switched off (MODEL_CALLS_VIA_SQUIRRELWISDOM). Caroline's own check was only "PIA
balance > 0" when a tab entered sw mode, so a tab already in sw mode kept spending
with an empty wallet: one account spent ~$34 of OpenRouter in two weeks against
~$4 charged to its wallet.

Now every call made through the adapter _model_ai() builds is
  - refused while the wallet is known to be empty (re-read from the server before
    giving up, so a top-up is seen at once), with the same "402 Insufficient funds"
    words the engine's own funds handling already waits on and reports;
  - charged right after it returns: the real OpenRouter cost (usage.cost) times
    MODEL_BILLING_MULTIPLIER, in PIA, through wallet:charge. allow_negative, because
    the call is already paid for -- the wallet goes below zero by at most one call
    and the next call is refused. notify off: wallet:charge mails every charge
    otherwise, and this is one charge per model call.

The balance is re-read from the server at the start of every sw turn (mark_stale);
between turns the balance each charge returns is kept.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import httpx

from app.http_tls import ssl_context
from app.logging_setup import log_event
from app.plugins.sw_api import API_URL, CAROLINE_SW_KEY
from app.sw_service_keys_local import V2_LOGIN_SERVICE_KEY

# Same markup the server's agentic dialog engine bills with (reforce Config.AI_AGENTIC_BILLING_MULTIPLIER).
MODEL_BILLING_MULTIPLIER = 1.6
PIA_PER_USD = 1000.0
CHARGE_TIMEOUT_S = 20.0
INSUFFICIENT_FUNDS_TEXT = "SquirrelWisdom returned 402 Insufficient funds: the piastre balance is empty"

_lock = threading.Lock()
_known_balance: float | None = None


def mark_stale() -> None:
    """The next call re-reads the balance from the server (the start of every sw turn)."""
    global _known_balance
    with _lock:
        _known_balance = None


def _post(body: dict[str, Any]) -> dict[str, Any]:
    last: Exception | None = None
    for _attempt in (1, 2):
        try:
            res = httpx.post(API_URL, json=body, timeout=CHARGE_TIMEOUT_S, verify=ssl_context())
            return res.json()
        except Exception as exc:  # noqa: BLE001 -- one retry, then the caller decides
            last = exc
    raise RuntimeError(f"{body.get('command')} failed: {last}")


def _read_balance(session: str) -> float:
    data = _post({"command": "wallet:getBalance", "key": V2_LOGIN_SERVICE_KEY, "session": session})
    if data.get(".status") != "ok":
        raise RuntimeError(f"wallet:getBalance failed: {data.get('.reason')}")
    return float((data.get("balances") or {}).get("PIA") or 0)


def ensure_funds(session: str) -> None:
    """Raises INSUFFICIENT_FUNDS_TEXT when the wallet is empty. A failed balance read
    does not block the call: an SW hiccup must not stop a paying user."""
    global _known_balance
    with _lock:
        known = _known_balance
    if known is not None and known > 0:
        return
    try:
        balance = _read_balance(session)
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "model_billing_balance_read_failed", error=str(exc)[:200])
        return
    with _lock:
        _known_balance = balance
    if balance <= 0:
        log_event("engine", "model_billing_refused", balance_pia=balance)
        raise RuntimeError(INSUFFICIENT_FUNDS_TEXT)


def _usage_cost(usage: Any) -> float:
    if usage is None:
        return 0.0
    cost = usage.get("cost") if isinstance(usage, dict) else getattr(usage, "cost", None)
    try:
        return float(cost or 0)
    except (TypeError, ValueError):
        return 0.0


def charge(session: str, model: str, usage: Any, call_id: str | None = None) -> None:
    """Charges one finished call. Never raises: the answer is already paid for and must
    not be lost; a failed charge is logged with its amount."""
    global _known_balance
    cost_usd = _usage_cost(usage)
    if cost_usd <= 0:
        return
    amount = max(0.01, round(cost_usd * MODEL_BILLING_MULTIPLIER * PIA_PER_USD, 2))
    body = {
        "command": "wallet:charge", "key": CAROLINE_SW_KEY, "session": session,
        "currency": "PIA", "amount": amount,
        "item_type": "caroline-model-call", "item_id": model,
        "description": f"Caroline model call: {model}",
        "idempotency_key": f"caroline-model-call:{call_id or uuid.uuid4().hex}",
        "allow_negative": True, "notify": False,
    }
    try:
        data = _post(body)
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "model_billing_charge_failed", model=model, amount_pia=amount, error=str(exc)[:200])
        return
    if data.get(".status") != "ok" or not data.get("result"):
        log_event("engine", "model_billing_charge_failed", model=model, amount_pia=amount,
                  error=str(data.get("message") or data.get(".reason"))[:200])
        return
    balance = data.get("balance_major")
    with _lock:
        if balance is not None:
            _known_balance = float(balance)
    log_event("engine", "model_billing_charged", model=model, cost_usd=cost_usd, amount_pia=amount, balance_pia=balance)


def session_of(api_key: str) -> str:
    """The session half of the composite "<CAROLINE_SW_KEY>.<session>" proxy token."""
    return api_key.split(".", 1)[1] if "." in api_key else api_key


class ChargingAdapter:
    """Wraps Camerlengo's OpenRouterAdapter: the same _resolve(), with ensure_funds()
    before the call and charge() after it. Everything else (client, model, ...) is the
    wrapped adapter's own. `api_key` is the composite "<CAROLINE_SW_KEY>.<session>"
    token the proxy takes (model_key_provisioning.py)."""

    def __init__(self, inner: Any, api_key: str) -> None:
        self._inner = inner
        self.session = session_of(api_key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _resolve(self, *args: Any, **kwargs: Any) -> Any:
        ensure_funds(self.session)
        response = self._inner._resolve(*args, **kwargs)
        model = kwargs.get("model") or (args[0] if args else None) or getattr(self._inner, "model", None) or "?"
        self.charge(str(model), response)
        return response

    def charge(self, model: str, response: Any) -> None:
        charge(self.session, model, getattr(response, "usage", None), getattr(response, "id", None))

"""Tripwire for app/microagent_client.py (2026-10-17 pilot -- see that
module's own docstring). Checks what's deterministic without a real network
call or a real Camerlengo: envelope unwrapping (makeResponse merges the
command's payload at the TOP level of the response, not nested -- a real,
caught-before-ship bug this guards against regressing), and the shipped/
dev-tree/override resolution precedence.

Run directly: python test_microagent_client.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import microagent_client as mc

_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    if not condition:
        _FAILURES.append(name)
        print(f"FAIL: {name}")
    else:
        print(f"ok:   {name}")


def test_llm_call_unwraps_flat_envelope():
    """makeResponse() (reforce's API/Api2Dispatcher.py) merges a command's
    payload dict at the TOP level of the response alongside .status/.msgid
    -- NOT nested under an "output" key. _llm_call must read text/usage
    from the top level."""
    captured = {}

    def fake_post_json_sync(body, timeout=30.0, retries=3):
        captured.update(body)
        return {".status": "ok", ".msgid": "x", "text": "hello", "usage": {"model": "fake/nano", "cost": 0.001}}

    real = mc._post_json_sync
    mc._post_json_sync = fake_post_json_sync
    try:
        result = mc._llm_call("nano", "a prompt", 10.0, reasoning="low")
    finally:
        mc._post_json_sync = real
    check("llm_call returns text from the flat envelope", result.get("text") == "hello")
    check("llm_call returns usage from the flat envelope", result.get("usage", {}).get("model") == "fake/nano")
    check("llm_call sent modelClass/prompt/timeoutS/reasoning", captured.get("modelClass") == "nano"
          and captured.get("prompt") == "a prompt" and captured.get("timeoutS") == 10.0 and captured.get("reasoning") == "low")
    check("llm_call sends the CAROLINE_SW_KEY", captured.get("key") == mc.CAROLINE_SW_KEY)


def test_llm_call_raises_on_error_envelope():
    def fake_post_json_sync(body, timeout=30.0, retries=3):
        return {".status": "error", ".reason": "insufficient funds"}

    real = mc._post_json_sync
    mc._post_json_sync = fake_post_json_sync
    try:
        try:
            mc._llm_call("nano", "a prompt", 10.0)
            check("llm_call raises on an error envelope", False)
        except RuntimeError as exc:
            check("llm_call raises on an error envelope", "insufficient funds" in str(exc))
    finally:
        mc._post_json_sync = real


def test_resolve_camerlengo_dir_precedence():
    with tempfile.TemporaryDirectory() as shipped_dir, tempfile.TemporaryDirectory() as override_dir:
        (Path(shipped_dir) / "Microagent.py").write_text("# fake", encoding="utf-8")
        (Path(override_dir) / "Microagent.py").write_text("# fake", encoding="utf-8")

        real_shipped = mc._SHIPPED_CAMERLENGO_DIR
        mc._SHIPPED_CAMERLENGO_DIR = Path(shipped_dir)
        try:
            check("shipped dir wins when no override is set", mc._resolve_camerlengo_dir() == Path(shipped_dir))
            os.environ["CAROLINE_CAMERLENGO_PATH"] = override_dir
            try:
                check("env override wins over the shipped dir", mc._resolve_camerlengo_dir() == Path(override_dir))
            finally:
                del os.environ["CAROLINE_CAMERLENGO_PATH"]
        finally:
            mc._SHIPPED_CAMERLENGO_DIR = real_shipped


if __name__ == "__main__":
    test_llm_call_unwraps_flat_envelope()
    test_llm_call_raises_on_error_envelope()
    test_resolve_camerlengo_dir_precedence()
    if _FAILURES:
        print(f"\n{len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nAll checks passed.")

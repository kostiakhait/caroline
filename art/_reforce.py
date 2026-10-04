"""Synchronous Camerlengo v2 calls for these art scripts. The key and URL are
read from backend-py/app/reforce_v2.py so there is one place they live."""

import importlib.util
import os

import requests

_spec = importlib.util.spec_from_file_location(
    "reforce_v2",
    os.path.join(os.path.dirname(__file__), "..", "backend-py", "app", "reforce_v2.py"),
)
_reforce_v2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_reforce_v2)


def v2(command, body, timeout=120):
    r = requests.post(
        _reforce_v2.REFORCE_URL + "/",
        json={"command": command, "key": _reforce_v2.REFORCE_KEY, **body},
        timeout=timeout,
    )
    data = r.json()
    if data.get(".status") != "ok":
        raise Exception(f"API error: {data}")
    return data

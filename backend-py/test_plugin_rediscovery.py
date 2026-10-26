"""A second discover_plugins() (every query build does one) must not reload modules already loaded: the sw_api exception
classes and module state stay the same objects for every module that imported them (2026-10-26).
Run: python -m pytest test_plugin_rediscovery.py -q"""

import asyncio
import importlib

import pytest

import app.evolution_client as evolution_client
from app.plugins import loader
from app.plugins import sw_api

# Modules that import the sw_api exceptions by name: loaded before sw_api in discovery order, after it, and outside the
# plugins package (never discovered at all).
IMPORTERS = ["memory_plugin", "speech_plugin", "companion_api", "calendar_plugin", "contacts_plugin",
             "visual_models_plugin", "wikipedia_plugin"]


@pytest.fixture
def rediscovered():
    loader.discover_plugins()
    before = {"SessionExpiredError": sw_api.SessionExpiredError, "SwApiError": sw_api.SwApiError}
    loader.discover_plugins()
    return before


def test_exception_classes_are_the_same_objects_everywhere(rediscovered):
    assert sw_api.SessionExpiredError is rediscovered["SessionExpiredError"]
    assert sw_api.SwApiError is rediscovered["SwApiError"]
    modules = [evolution_client] + [importlib.import_module(f"app.plugins.{name}") for name in IMPORTERS]
    for module in modules:
        for cls in ("SessionExpiredError", "SwApiError"):
            if hasattr(module, cls):
                assert getattr(module, cls) is getattr(sw_api, cls), f"{module.__name__}.{cls} is stale"


def test_an_expired_session_raised_by_a_plugin_is_retried(rediscovered):
    memory = importlib.import_module("app.plugins.memory_plugin")
    manager = sw_api.SessionManager()
    manager._session = "old"

    async def ensure_session():
        return manager._session or "new"

    manager.ensure_session = ensure_session
    attempts = []

    async def call(session):
        attempts.append(session)
        if len(attempts) == 1:
            raise memory.SessionExpiredError("Unknown or expired session")
        return "ok"

    assert asyncio.run(manager.with_session(call)) == "ok"
    assert attempts == ["old", "new"]


def test_module_state_survives_a_rediscovery(monkeypatch):
    monkeypatch.setattr(sw_api, "_funds_exhausted_reason", None)
    sw_api.mark_funds_exhausted("balance too low")
    memory = importlib.import_module("app.plugins.memory_plugin")
    sessions = memory._sessions
    loader.discover_plugins()
    assert sw_api.get_funds_exhausted_reason() == "balance too low"
    assert memory._sessions is sessions                       # a plugin's SW session is not dropped either


def test_a_rediscovery_still_finds_every_plugin():
    first = sorted(p.name for p in loader.discover_plugins())
    assert first and sorted(p.name for p in loader.discover_plugins()) == first

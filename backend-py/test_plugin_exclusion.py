"""CAROLINE_EXCLUDED_PLUGINS leaves named plugins out of discover_plugins(); unset leaves everything in."""
from app.plugins import loader


def test_excluded_plugins_are_left_out(monkeypatch):
    monkeypatch.delenv("CAROLINE_EXCLUDED_PLUGINS", raising=False)
    everything = {p.name for p in loader.discover_plugins()}
    assert "voice-playback" in everything

    monkeypatch.setenv("CAROLINE_EXCLUDED_PLUGINS", " voice-playback , visual-models ,")
    kept = {p.name for p in loader.discover_plugins()}
    assert "voice-playback" not in kept and "visual-models" not in kept
    assert kept == everything - {"voice-playback", "visual-models"}


def test_unset_excludes_nothing(monkeypatch):
    monkeypatch.delenv("CAROLINE_EXCLUDED_PLUGINS", raising=False)
    assert loader.excluded_plugin_names() == set()

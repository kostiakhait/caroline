"""The role of Caroline in a product that hosts her for one job (opt-in).

A host that runs Caroline for one product -- today only the Ferret browser editor (ferret/tools/caroline-host) -- puts the text of her role in
a file and gives its path as CAROLINE_PRODUCT_ROLE_FILE. The text goes into every engine's system prompt (Claude, Codex, the SW small-model
engine) right after her identity, and replaces the "personal assistant / secretary" framing there. Nothing sets the variable on her other
platforms: there this module returns None and nothing changes."""

from __future__ import annotations

import os
from pathlib import Path

ENV = "CAROLINE_PRODUCT_ROLE_FILE"


def role_text() -> str | None:
    path = os.environ.get(ENV)
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def role_clause() -> str | None:
    """For the engines whose identity text calls her a personal assistant/secretary: the product role, stated as overriding that."""
    text = role_text()
    if not text:
        return None
    return ("YOUR ROLE IN THIS PRODUCT (it replaces \"personal assistant\" and \"personal secretary\" above: here you are not an assistant "
            "and not a secretary; keep your name and the grammatical gender above):\n" + text)

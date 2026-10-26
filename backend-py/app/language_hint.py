"""The language of a turn is the language the user writes in (decided 2026-10-25: a reply to a user who wrote in
Russian came out in English on a fresh tab, because the tab's language is only resolved after its first turn and
current_language_name() answers "English" until then).

This is the instant, offline part: the letters of the message itself. The resolved language (a model call, see
refresh_language_in_background) still wins over this one on later turns."""

from __future__ import annotations


def guess_language_from_text(text: str) -> str | None:
    """Russian or Ukrainian when the message is mostly Cyrillic, else None (not enough to say)."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 2:
        return None
    cyrillic = [c for c in letters if "\u0400" <= c <= "\u04ff"]
    if len(cyrillic) / len(letters) < 0.5:
        return None
    return "Ukrainian" if any(c in "іїєґІЇЄҐ" for c in cyrillic) else "Russian"

"""The language of a turn is the language the user writes in (decided 2026-10-25: a reply to a user who wrote in
Russian came out in English on a fresh tab, because the tab's language is only resolved after its first turn and
current_language_name() answers "English" until then).

This is the instant, offline part: the letters of the message itself. The resolved language (a model call, see
refresh_language_in_background) still wins over this one on later turns."""

from __future__ import annotations

import re as _re

_CYRILLIC = _re.compile("[\u0400-\u04ff]")
_NOT_RUSSIAN_LETTERS = _re.compile("[\u0456\u0457\u0454\u0491\u045e]", _re.IGNORECASE)
_VERBATIM = _re.compile(r"`[^`]*`|https?://\S+|\S+@\S+")


def already_in_language(text: str, language: str) -> bool:
    """True when a reply plainly needs no translation, so the translator is not asked (2026-11-27, live: two translation
    calls, 9 seconds, for a Russian reply in a Russian dialogue). Decided only for Russian, where the script tells: the
    text has Cyrillic, no letters of the neighbouring alphabets, and no sentence of three or more Latin words without any
    Cyrillic (the stray English status line the translator exists for). Any doubt means: translate."""
    if (language or "").strip().lower() != "russian" or _NOT_RUSSIAN_LETTERS.search(text or ""):
        return False
    plain = _VERBATIM.sub(" ", text or "")
    if not _CYRILLIC.search(plain):
        return False
    return not any(
        not _CYRILLIC.search(sentence) and len(_re.findall(r"[A-Za-z]{2,}", sentence)) >= 3
        for sentence in _re.split(r"[.!?\u2026\n]+", plain)
    )


def guess_language_from_text(text: str) -> str | None:
    """Russian or Ukrainian when the message is mostly Cyrillic, else None (not enough to say)."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 2:
        return None
    cyrillic = [c for c in letters if "\u0400" <= c <= "\u04ff"]
    if len(cyrillic) / len(letters) < 0.5:
        return None
    return "Ukrainian" if any(c in "іїєґІЇЄҐ" for c in cyrillic) else "Russian"

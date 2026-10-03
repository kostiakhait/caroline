"""Tiny local cache of the owner's own quick-reference facts (name, gender,
age) for system-prompt injection. This is deliberately NOT the source of
truth: per explicit instruction (2026-10-03), the real/full owner profile
(biography, requisites, etc.) lives in the "Caroline:Profile" Notes folder
(see policies.py's owner_profile_instruction) and should be kept in sync
with the owner's own entry in the address book (contacts_plugin.py) where
one exists -- this module only holds the small, bounded subset of that same
data that needs to be in EVERY system prompt, because grammatical gender
agreement when addressing the user matters on every single reply in a
gendered language, not just when some other biographical detail happens to
come up (the same reasoning persona.py's own always-on gender-agreement
clause already relies on, just applied to the OWNER instead of to Caroline
herself).

Confirmed live (2026-10-03) as a real, distinct bug: persona.py's
persona_system_prompt_append already has a fixed rule for Caroline's OWN
self-referential gender agreement, but nothing at all told her the OWNER's
gender -- she had no grounding for second/third-person verb agreement when
addressing or referring to THEM in Russian, and was observed bleeding her
own feminine self-agreement onto the user instead.

Same local-JSON-cache shape persona.py already uses for Caroline's own
identity fields (_persona_path/_load_stored_file/_save_stored_file), applied
here to a different, much smaller fact set -- deliberately NOT a live Notes/
Contacts network fetch on every system-prompt build: a past attempt to
inline dynamic Notes content into every system prompt (the original, since-
reverted version of owner_profile_instruction) measurably contributed to a
real --append-system-prompt command-line-length overflow (~32K chars, at
Windows' CreateProcess limit) that broke every tab's connection. A fixed,
tiny fact (one name, one word for gender, one age) is a different risk class
entirely from inlining a whole growing Notes folder, but the FETCH mechanism
(local file, not network) stays the same precaution regardless.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OwnerProfile:
    name: str = ""
    gender: str = ""
    age: str = ""

    @property
    def is_set(self) -> bool:
        return bool(self.name or self.gender)


def _owner_profile_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "owner_profile.json"


def get_owner_profile(workspace_dir: str) -> OwnerProfile:
    try:
        stored = json.loads(_owner_profile_path(workspace_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        stored = {}
    return OwnerProfile(name=stored.get("name") or "", gender=stored.get("gender") or "", age=stored.get("age") or "")


def save_owner_profile(workspace_dir: str, name: str | None = None, gender: str | None = None, age: str | None = None) -> OwnerProfile:
    """Partial update, same convention as contacts_plugin.py's contact_update -- only
    fields actually passed (not None) overwrite the stored value, so Caroline can fix
    just one field (e.g. a corrected age) without re-stating what she already knows."""
    current = get_owner_profile(workspace_dir)
    updated = OwnerProfile(
        name=name if name is not None else current.name,
        gender=gender if gender is not None else current.gender,
        age=age if age is not None else current.age,
    )
    _owner_profile_path(workspace_dir).write_text(
        json.dumps({"name": updated.name, "gender": updated.gender, "age": updated.age}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return updated


def owner_profile_system_prompt_clause(profile: OwnerProfile) -> str:
    """Always-on (called from chat_session.py's own system_prompt_parts, right next to
    persona_system_prompt_append) -- deliberately tiny, see this module's own doc comment
    for why that's safe here despite the system-prompt-overflow precedent. Explicitly
    contrasts with persona_system_prompt_append's OWN self-gender-agreement rule, since
    conflating "my gender" with "the person I'm talking to's gender" is exactly the
    observed bug this exists to fix."""
    if profile.is_set:
        facts = ", ".join(p for p in (profile.name, profile.age, profile.gender) if p)
        return (
            f"The person you are assisting (your owner) is {facts}. This is a SEPARATE fact from your own "
            "gender above -- when addressing or referring to THEM (second/third person: \"ты\", \"он\"/\"она\", "
            "a past-tense verb about something THEY did), use grammatical forms matching THEIR gender, not "
            "yours. In Russian specifically: a male owner gets \"ты сделал\"/\"ты сказал\" (never \"сделала\"/"
            "\"сказала\"), a female owner the reverse. Don't let your own self-referential gender agreement "
            "bleed into how you address them -- these are two independent facts about two different people, "
            "and both matter every single reply, not just when one or the other is the explicit topic."
        )
    return (
        "You don't yet know your owner's name/gender/age -- separate from your own gender above, and it "
        "matters for correct grammatical agreement when addressing or referring to THEM (e.g. \"ты сделал\" "
        "vs \"ты сделала\" in Russian). Figure it out from the conversation, the \"Caroline:Profile\" Notes "
        "folder, or their own entry in your address book if one exists, then save it with set_owner_profile "
        "as soon as you know it -- this is one-time setup worth resolving early, not something to leave "
        "unresolved indefinitely. Until you know it, don't assume it matches your own gender just because "
        "that's the one you're certain of -- ask directly if a reply genuinely needs a gendered construction "
        "and you can't tell yet, rather than guessing."
    )

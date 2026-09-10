"""Minimal slice of backend/src/persona.ts -- ONLY the gender field, needed
by voice_api.py's TTS voice selection (voice_for_gender). The full persona
system (curated Caroline/Peter identities, custom personas, biography,
photos, the persona_get/persona_set control ops) is NOT ported yet --
tracked separately as remaining Phase 3/4 work, not silently dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

_STANDARD_GENDERS = {"caroline": "female", "peter": "male"}
_DEFAULT_CUSTOM_GENDER = "female"


def get_persona_gender(workspace_dir: str) -> str:
    path = Path(workspace_dir) / "persona.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        stored = {}
    profile_key = stored.get("profileKey") or "caroline"
    if profile_key == "custom":
        return (stored.get("custom") or {}).get("gender") or _DEFAULT_CUSTOM_GENDER
    override = (stored.get("overrides") or {}).get(profile_key) or {}
    return override.get("gender") or _STANDARD_GENDERS.get(profile_key, "female")

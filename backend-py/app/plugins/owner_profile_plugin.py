"""owner_profile -- the tiny, always-injected subset (name/gender/age) of
what Caroline knows about her owner. See app/owner_profile.py's own doc
comment for why this is separate from the full biography, which stays in
the "Caroline:Profile" Notes folder (policies.py's owner_profile_instruction)
and is fetched on demand like everything else there.

There is no separate settings-UI path for this, by design (unlike
persona.py's own Settings-driven override mechanism for Caroline's OWN
identity) -- Caroline maintains it herself: she determines these facts from
conversation, "Caroline:Profile", or the owner's own contact entry in the
address book (contacts_plugin.py, if available) and keeps set_owner_profile
in sync with whichever of those she touches.
"""

from __future__ import annotations

import json
from typing import Any

from app.owner_profile import get_owner_profile, save_owner_profile
from app.plugins.loader import Plugin, PluginTool
from app.workspace_dir import WORKSPACE_DIR


async def owner_profile_get(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    profile = get_owner_profile(WORKSPACE_DIR)
    return {"text": json.dumps({"name": profile.name, "gender": profile.gender, "age": profile.age, "isSet": profile.is_set}, ensure_ascii=False)}


async def owner_profile_set(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    profile = save_owner_profile(WORKSPACE_DIR, name=args.get("name"), gender=args.get("gender"), age=args.get("age"))
    return {"text": json.dumps({"name": profile.name, "gender": profile.gender, "age": profile.age}, ensure_ascii=False)}


_USAGE_INSTRUCTIONS = (
    "This is ONLY the small quick-reference set (name/gender/age) that gets baked into every system prompt "
    "for grammatical gender agreement when addressing your owner -- it is NOT where their full biography/"
    "requisites live (that's the \"Caroline:Profile\" Notes folder, read via notes_list/notes_get). If it's "
    "unset, or you learn the owner's name/gender/age from conversation, from \"Caroline:Profile\", or from "
    "their own entry in your address book (contact_search/contact_get, if the contacts tools are available), "
    "call owner_profile_set right away -- don't leave it unresolved. Keep all three places (this cache, "
    "\"Caroline:Profile\", and the owner's own contact entry if one exists) saying the same thing: if you "
    "update one because you learned something new or corrected, update the others too rather than letting "
    "them drift apart. age can be an approximate description (e.g. \"around 40\") if you don't know it "
    "exactly -- don't ask just to fill this field precisely if it's not otherwise relevant."
)


PLUGIN = Plugin(
    name="owner_profile",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool(
            "owner_profile_get",
            "Reads the currently cached owner name/gender/age (the small always-in-system-prompt subset, "
            "not the full biography).",
            {}, owner_profile_get,
        ),
        PluginTool(
            "owner_profile_set",
            "Updates the cached owner name/gender/age. Partial update -- only the fields you pass change.",
            {"name": str | None, "gender": str | None, "age": str | None}, owner_profile_set,
        ),
    ],
)

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
from app.plugins.memory_plugin import request_owner_profile, save_owner_profile_text
from app.workspace_dir import WORKSPACE_DIR


async def owner_profile_get(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    profile = get_owner_profile(WORKSPACE_DIR)
    return {"text": json.dumps({"name": profile.name, "gender": profile.gender, "age": profile.age, "isSet": profile.is_set}, ensure_ascii=False)}


async def owner_profile_set(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    profile = save_owner_profile(WORKSPACE_DIR, name=args.get("name"), gender=args.get("gender"), age=args.get("age"))
    return {"text": json.dumps({"name": profile.name, "gender": profile.gender, "age": profile.age}, ensure_ascii=False)}


async def owner_profile_remember(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await save_owner_profile_text(args.get("text") or "")


async def owner_profile_recall(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await request_owner_profile(args.get("query") or "")


_USAGE_INSTRUCTIONS = (
    "Your owner's profile has two parts. owner_profile_get/owner_profile_set are ONLY the small quick-reference "
    "set (name/gender/age) baked into every system prompt for grammatical gender agreement when addressing your "
    "owner. Everything else about your owner -- who they are, what they prefer, their personal details and "
    "requisites -- is kept with owner_profile_remember(text) and found with owner_profile_recall(query) (the "
    "\"Caroline:Profile\" Notes folder behind them; the text is appended to the fitting note, never rewriting "
    "it). These are deliberately separate from save_info/request_info: use them when, and only when, what you "
    "are keeping or looking for is about your OWNER themselves -- never for other people, even ones close to "
    "them. If the quick-reference set is "
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
            "owner_profile_remember",
            "Keeps something about your OWNER themselves -- who they are, a preference, a personal detail -- in "
            "their profile. Only for your owner, never for other people (those go to save_info). \"text\" is "
            "what to keep, in your own words, complete enough to make sense on its own later.",
            {"text": str}, owner_profile_remember,
        ),
        PluginTool(
            "owner_profile_recall",
            "Looks something up in your OWNER's profile -- who they are, their preferences, personal details. "
            "request_info does not search it: when the question is about your owner, call this.",
            {"query": str}, owner_profile_recall,
        ),
        PluginTool(
            "owner_profile_set",
            "Updates the cached owner name/gender/age. Partial update -- only the fields you pass change.",
            {"name": str | None, "gender": str | None, "age": str | None}, owner_profile_set,
        ),
    ],
)

"""memory-search -- recall_memory: a fast, dedicated retrieval tool over the
whole Notes account, so finding something never costs the MAIN conversation's
own context/turns. See policies.py's thematic_memory_convention_instruction
(the "Caroline:Topics" saving convention, on-demand) and
recall_memory_check_first_instruction (the always-on trigger) for the "why"
-- this file is purely the retrieval side.

Root motivation (2026-09-24, the user's own words): the main model already
knows, in principle, where things are in Notes -- it just doesn't reliably
have the context/attention budget left, mid-conversation, to browse/search
correctly. So the search itself has to happen in a SEPARATE call, in its own
context, never as part of the main model's own reasoning -- deliberately no
string/regex/substring matching in this file either: a cheap, single-shot LLM
call picks which note(s) (by TITLE only, not full text -- keeps this pass
cheap regardless of account size) are worth reading in full; this tool then
fetches and returns just those. The main model still reads and reasons over
the actual returned content itself -- the sub-call's only job is navigation/
selection, not final synthesis, so a second layer of hallucination can't
silently replace the real note text.

recall_memory is ADDITIVE: every notes_* tool (notes_search, notes_list,
notes_get, ...) remains fully available and unchanged for direct/addressed
lookups where the model already knows exactly where to look.

Which engine answers the cheap selection call follows the CALLING TAB'S own
current mode (durability.load_chat_mode: "claude"/"sw"/"openai"):
  - "sw" and "openai" both go through Camerlengo's ai:resolve (model="SMALL",
    the existing fast/cheap tier -- see voice_api.py's own use of it).
    "openai" is a pragmatic fallback here, not a dedicated OpenAI/Codex path:
    no lightweight single-shot OpenAI primitive exists in this codebase
    today (CodexRpcClient is the full conversational RPC session used for
    real turns, not a cheap one-shot lookup) -- building one is a separate,
    larger piece of work, out of scope for this pass.
  - "claude" goes through a one-shot claude_agent_sdk.query() with
    model="haiku" (the plain alias already used elsewhere, see
    subscription_mode.py's CLAUDE_MODEL_ALIASES) and no MCP tools attached --
    pure text-in/text-out over the provided title list.

Notes access itself uses notes_api.py's own SessionManager/with_session
directly (own instance here, per this file family's established per-plugin
convention -- see notes_plugin.py), the SAME as every other notes_* tool --
deliberately NOT consult_plugin.py's require_sw_or_prompt auto-popup gate,
since this is fundamentally a Notes tool, not a consult-a-model tool, and
every other Notes tool already surfaces "not logged in" as a plain error
for the model to act on (e.g. call ensure_squirrelwisdom_login) rather than
interrupting the user with a native window on every call.
"""

from __future__ import annotations

from typing import Any

from app.durability import load_chat_mode
from app.plugins.loader import Plugin, PluginTool
from app.plugins.notes_api import SessionManager
from app.plugins.notes_plugin import get_note, list_notes
from app.plugins.sw_api import CAROLINE_SW_KEY, call_v2
from app.session_context import get_tab_id
from app.workspace_dir import WORKSPACE_DIR

_sessions = SessionManager()

_NO_SELECTION = "NONE"


def _title_of(text: str) -> str:
    return text.split("\n", 1)[0]


def _selection_prompt(query: str, candidates: list[dict[str, Any]]) -> str:
    listing = "\n".join(f'{c["id"]}: "{c["title"]}" (folder: {c["folder"] or "(none)"})' for c in candidates)
    return (
        "Below is a list of note titles from someone's personal knowledge base, each with its id and folder. "
        f'Given this query: "{query}"\n\n'
        "Which note(s), if any, are actually relevant? Reply with ONLY a comma-separated list of their ids "
        f'(e.g. "abc123,def456"), or the single word {_NO_SELECTION} if none are relevant. Nothing else -- no '
        "explanation, no punctuation beyond the commas.\n\n"
        f"{listing}"
    )


def _parse_ids(answer: str, valid_ids: set[str]) -> list[str]:
    stripped = answer.strip()
    if not stripped or stripped.upper() == _NO_SELECTION:
        return []
    # Ignore anything the sub-call hallucinated that isn't a real id -- never
    # trust it blindly, this only ever narrows down to notes that actually exist.
    return [part.strip() for part in stripped.split(",") if part.strip() in valid_ids]


async def _select_via_sw(query: str, candidates: list[dict[str, Any]], session: str) -> list[str]:
    result = await call_v2("ai:resolve", key=CAROLINE_SW_KEY, question=_selection_prompt(query, candidates), model="SMALL", session=session)
    answer = result.get("result")
    if not isinstance(answer, str):
        raise RuntimeError("recall_memory's search call (ai:resolve) returned no result text")
    return _parse_ids(answer, {c["id"] for c in candidates})


async def _select_via_claude(query: str, candidates: list[dict[str, Any]]) -> list[str]:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock
    from claude_agent_sdk import query as sdk_query

    options = ClaudeAgentOptions(
        cwd=WORKSPACE_DIR, model="haiku", max_turns=1, mcp_servers={},
        permission_mode="bypassPermissions", extra_args={"strict-mcp-config": None},
    )
    answer = ""
    async for message in sdk_query(prompt=_selection_prompt(query, candidates), options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    answer += block.text
    return _parse_ids(answer, {c["id"] for c in candidates})


async def recall_memory(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    query = args["query"]
    # with_session (not a raw ensure_session): the standard notes_plugin.py shape,
    # transparently re-logs in once on a stale/expired cached session -- raises
    # NotesApiError with a clear message if not logged in at all.
    entries = await _sessions.with_session(lambda session: list_notes(session, folder=None, include_deleted=False))
    candidates = [{"id": e["id"], "title": _title_of(e.get("text", "")), "folder": e.get("folder") or ""} for e in entries]
    if not candidates:
        return {"text": "Nothing is stored in Notes yet."}

    mode = load_chat_mode(WORKSPACE_DIR, get_tab_id() or "")
    try:
        if mode == "claude":
            ids = await _select_via_claude(query, candidates)
        else:
            _email, _hash16, session = await _sessions.ensure_session()
            ids = await _select_via_sw(query, candidates, session)
    except Exception as exc:
        return {"text": f'recall_memory\'s search failed: {exc}. Your other notes_* tools (notes_search, notes_list) still work directly.', "is_error": True}

    if not ids:
        return {"text": f'Nothing relevant found for "{query}" across {len(candidates)} stored note(s).'}

    by_id = {c["id"]: c for c in candidates}
    bodies = []
    for note_id in ids:
        note = await _sessions.with_session(lambda session, nid=note_id: get_note(session, nid))
        bodies.append(f'"{by_id[note_id]["title"]}" (folder: {by_id[note_id]["folder"] or "(none)"}):\n{note.get("text", "")}')
    return {"text": "\n\n---\n\n".join(bodies)}


PLUGIN = Plugin(
    name="memory-search",
    tools=[
        PluginTool(
            "recall_memory",
            "Searches your ENTIRE Notes account (not just one folder) for whatever might be relevant to a topic "
            "or keyword, using a separate fast model call to pick likely notes by title -- so it costs your own "
            "context/turns nothing but the answer, unlike browsing folders yourself. Prefer this over "
            "notes_search/notes_list for anything you're not sure exists or don't know the exact folder for; "
            "your other notes_* tools remain fully available for direct/addressed lookups.",
            {"query": str}, recall_memory,
        ),
    ],
)

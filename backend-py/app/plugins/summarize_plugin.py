"""summarize -- a single tool, summarize_content, over Camerlengo's
content.summarize microagent (reforce's apps/microagents/ContentSummary.py,
reply:summarize command). Added 2026-10-16 alongside that day's context-
overflow pass: app/context_cap.py's truncate() gives a raw PREFIX window of
anything too long -- useful when exact wording matters, a blunt instrument
when only the gist is needed (a long email thread, a dehydrated-
conversation chunk from archive_chunks.py) and reading it piece by piece
would cost several tool calls' worth of your own context for no real gain.

Same SW-gate/credentials shape as consult_plugin.py's consult_large_model
(the closest existing precedent: one tool, one ai:*-family call, gated on
the user's own SquirrelWisdom login) -- reused directly rather than
reinvented.

path mode never puts the raw file content into your own context: the file
is read HERE and sent straight to the summarizer, the same principle
files_plugin.py's describe_local_image already applies to images -- only
the summary comes back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.context_cap import truncate
from app.evolution_client import run_reply_agent
from app.microagents.content_summary import SUMMARIZE
from app.plugins.files_plugin import MAX_READ_BYTES
from app.plugins.loader import Plugin, PluginTool
from app.plugins.notes_api import load_credentials, verify_password
from app.session_context import get_send
from app.sw_gate import require_sw_or_prompt


async def summarize_content(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    text = args.get("text")
    path = args.get("path")
    if bool(text) == bool(path):
        return {"text": 'summarize_content needs EXACTLY ONE of "text" or "path".', "is_error": True}
    if path:
        p = Path(path)
        if not p.exists():
            return {"text": f"No such file: {p}", "is_error": True}
        if not p.is_file():
            return {"text": f"Not a file: {p}", "is_error": True}
        data = p.read_bytes()
        text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")

    gate = await require_sw_or_prompt(get_send())
    if not gate.ok:
        return {"text": gate.message, "is_error": True}
    creds = load_credentials()
    assert creds is not None  # gate.ok guarantees this
    await verify_password(creds["email"], creds["password"])

    agent_input: dict[str, Any] = {"text": text}
    if args.get("target_chars"):
        agent_input["target_chars"] = int(args["target_chars"])
    result = await run_reply_agent(SUMMARIZE, agent_input)
    summary = (result.get("output") or {}).get("summary") if result.get("status") == "ok" else None
    if not isinstance(summary, str):
        raise RuntimeError("summarize_content (content.summarize) failed: no summary returned")
    # Defensive cap, same reasoning as consult_large_model's own: nothing upstream guarantees
    # the summary actually stayed short, even though the microagent's own verify_summarize
    # rejects a reply that's barely shorter than the original.
    window, note = truncate(summary)
    return {"text": window if note is None else window + "\n\n[... summary truncated: unusually long reply ...]"}


def _usage_instructions() -> str:
    return (
        "You have a summarize_content tool that gets the GIST of a long piece of text in one cheap call, "
        "instead of reading it in full or piece by piece. Use it when you only need to know what something "
        "SAYS, not its exact wording -- a long email thread before deciding whether to read it in full, an "
        "archived conversation chunk (see a dehydration index's own chunk files) before Reading it directly, "
        "any text whose tool result came back truncated or said it stopped early and you want the shape of "
        "what you're missing before deciding whether it's worth fetching the rest. Pass `text` for something "
        "already in your hands, or `path` for a local file -- path mode reads the file itself and never puts "
        "its raw content into your own context, only the summary comes back. Do NOT use this when the exact "
        "wording matters (quoting someone, citing a figure precisely, drafting a reply to specific points) -- "
        "a summary can drop details you'd need. Only available when the user is logged into their own "
        "SquirrelWisdom account (returns an error otherwise -- if that happens, read the text directly instead, "
        "in parts if it's long, same as you would have before this tool existed)."
    )


PLUGIN = Plugin(
    name="summarize",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "summarize_content",
            "Summarizes one long piece of text to roughly target_chars (default ~1000), keeping concrete "
            "facts/names/numbers/dates and dropping the rest -- for when the gist is enough. Give EXACTLY "
            "ONE of text (already in hand) or path (a local file, read here and never put into your own "
            "context raw). Only available when the user is logged into their own SquirrelWisdom account.",
            {"text": str | None, "path": str | None, "target_chars": int | None}, summarize_content,
        ),
    ],
)

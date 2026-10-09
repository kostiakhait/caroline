"""The older memory tools are deprecated and read-only.

Memory is now save_info / request_info (plugins/memory_plugin.py, see
docs/MICROAGENTS_PLAN.md). The tools that came before it -- recall_memory,
working_memory_*, topic_upsert/topics_list/topic_close, event_memory_* --
are all kept, so nothing they hold is lost, but for the model they are
read-only: the reading ones still work and say they are deprecated, the
writing ones refuse and point at save_info.

Only the model-facing tools are affected. Code that keeps these stores on
its own (schedule_reminder writing event memory) works as before.
"""

from __future__ import annotations

from typing import Any

READ_PREFIX = "DEPRECATED (read-only, older memory): prefer request_info. "
WRITE_PREFIX = "DEPRECATED and DISABLED: the older memory is read-only now; use save_info instead. "

USAGE_NOTE = (
    "DEPRECATED: this is the older memory, kept read-only. What it already holds can still be read with its "
    "reading tools; nothing new is written through it. To remember something use save_info, to recall something "
    "use request_info first (call get_tool_instructions on either for how)."
)


def read_only_refusal(tool_name: str) -> dict[str, Any]:
    return {
        "text": f"{tool_name} is disabled: the older memory is read-only now. Nothing was changed. "
                "Use save_info to remember this instead.",
        "is_error": True,
    }

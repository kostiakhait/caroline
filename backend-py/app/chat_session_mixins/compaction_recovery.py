from __future__ import annotations

import asyncio

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header
from app import compaction_guard

COMPACTION_MODEL = "sonnet"
SUMMARY_CHECK_DELAY_S = 1.5
STATE_DIALOGUE_MESSAGES = 20


class CompactionRecoveryMixin:
    """Compaction that compacts, not clears (app/compaction_guard.py): the model that writes the summary is a strong one,
    it is given the summarization instruction, the result is checked, and an unusable summary is replaced by one rebuilt from
    the exact record, handed over with the next message. Nothing here can fail a turn."""

    _context_recovery: str | None = None

    async def _run_forced_compaction(self) -> None:
        """Sets the model that writes the summary, then sends /compact with its instruction."""
        try:
            if self._ladder_active():           # a model pinned in Settings is left as the user chose it
                await self._ladder_set_model(COMPACTION_MODEL)
        except Exception as exc:  # noqa: BLE001
            log_event("engine", "compaction_model_switch_failed", tab_id=self.tab_id, error=str(exc)[:200])
        self._push_internal_command(compaction_guard.COMPACT_COMMAND)

    def _schedule_compaction_check(self) -> None:
        asyncio.get_running_loop().create_task(self._check_compaction_summary())

    async def _check_compaction_summary(self) -> None:
        try:
            await asyncio.sleep(SUMMARY_CHECK_DELAY_S)       # the CLI writes the summary right after the boundary
            if not self.last_saved_session_id:
                return
            path = claude_project_dir(self.workspace_dir) / f"{self.last_saved_session_id}.jsonl"
            summary, boundary, rows = compaction_guard.last_summary(path)
            degenerate = compaction_guard.is_degenerate(summary)
            log_event("engine", "compaction_summary_checked", tab_id=self.tab_id, chars=len(summary or ""), degenerate=degenerate,
                      found=summary is not None)
            if not degenerate or boundary is None:
                return
            digest = compaction_guard.digest_before_boundary(rows, boundary)
            if not digest.strip():
                return
            from app.small_model_engine import summarize_conversation

            rebuilt = await summarize_conversation(digest, self.tab_id) or digest[-8000:]
            self._context_recovery = compaction_guard.recovery_block(rebuilt, self._compaction_state_text())
            log_event("engine", "compaction_summary_recovered", tab_id=self.tab_id, summary_chars=len(summary or ""),
                      rebuilt_chars=len(rebuilt), digest_chars=len(digest))
        except Exception as exc:  # noqa: BLE001
            log_event("engine", "compaction_summary_check_failed", tab_id=self.tab_id, error=str(exc)[:200])

    def _compaction_state_text(self) -> str:
        """The facts the system itself holds, written after the rebuilt summary: open actions, finished actions, and the exact
        last messages of the dialogue as the user saw them."""
        parts: list[str] = []
        try:
            open_actions = [a.summary for a in self.action_registry.open_actions()]
            if open_actions:
                parts.append("OPEN ACTIONS (waiting for the user's decision or approval):\n" + "\n".join(f"- {s}" for s in open_actions))
        except Exception:  # noqa: BLE001
            pass
        try:
            from app import action_ledger, dialogue_log

            done = action_ledger.checker_note(self.workspace_dir)
            if done:
                parts.append(done)
            entries = dialogue_log.select(dialogue_log.read_entries(self.workspace_dir, self.tab_id),
                                          limit=STATE_DIALOGUE_MESSAGES, include_withheld=False)
            text = dialogue_log.render(entries)
            if text != "(no messages match)":
                parts.append("THE LAST MESSAGES, exactly as the user saw them (oldest first):\n" + text)
        except Exception:  # noqa: BLE001
            pass
        return "\n\n".join(parts)

    def _glue_context_recovery(self, item: dict[str, Any]) -> None:
        """The rebuilt context goes in as one more text block of the next message to the model."""
        block, self._context_recovery = self._context_recovery, None
        if not block:
            return
        try:
            content = item["message"]["message"]["content"]
            first = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
            if first.startswith("/"):
                self._context_recovery = block       # a command, not a message: the next one carries it
                return
            content.append({"type": "text", "text": _SYNTHETIC_TURN_MARKER + block})
            log_event("engine", "compaction_recovery_glued", tab_id=self.tab_id, chars=len(block))
        except Exception as exc:  # noqa: BLE001
            self._context_recovery = block
            log_event("engine", "compaction_recovery_glue_failed", tab_id=self.tab_id, error=str(exc)[:200])

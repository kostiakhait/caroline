from __future__ import annotations

import os

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header
from app import claude_ladder


class LadderMixin:
    """The model ladder of a Claude tab (app/claude_ladder.py): every turn starts on Haiku, an outside verifier
    decides whether the attempt finished the task, and if not the same session goes on with the next model.
    Hooks: _ladder_before_yield (_input_stream), _ladder_observe / _ladder_hold (the message loop, per wire message),
    _ladder_on_result (the loop, on a ResultMessage). Every failure falls back to "no ladder for this turn"."""

    _ladder: claude_ladder.LadderTurn | None = None
    _ladder_model: str | None = None     # the model the live client is set to (None: the CLI's own default)

    def _ladder_reset(self) -> None:
        self._ladder = None
        self._ladder_model = None

    def _ladder_active(self) -> bool:
        if os.environ.get("CAROLINE_NO_LADDER") == "1":
            return False
        override = get_model_override(self.workspace_dir, "claude")
        return claude_ladder.ladder_enabled(override, getattr(self, "engine_kind", ""))

    async def _ladder_set_model(self, model: str) -> bool:
        if self._ladder_model == model:
            return True
        client = getattr(self, "client", None)
        try:
            await client.set_model(model)
        except Exception as exc:  # noqa: BLE001 -- no ladder, the turn runs on whatever the client has
            log_event("engine", "claude_ladder_set_model_failed", tab_id=self.tab_id, model=model, error=str(exc)[:200])
            return False
        self._ladder_model = model
        return True

    async def _ladder_before_yield(self, item: dict[str, Any]) -> None:
        """Called by _input_stream for each queued message just before the SDK gets it."""
        try:
            if item.get("ladder_continuation"):
                return  # the next rung of the turn in progress: the model was set when the verdict came
            self._ladder = None
            if not self._ladder_active():
                # A model pinned in Settings applies from this message on, also to a client the ladder had moved.
                pinned = get_model_override(self.workspace_dir, "claude")
                if pinned and getattr(self, "engine_kind", "") == "claude" and self._ladder_model not in (None, pinned):
                    await self._ladder_set_model(pinned)
                return
            text = claude_ladder.user_text_of(item["message"]) or item.get("memory_advice_for") or ""
            turn = claude_ladder.LadderTurn(user_text=text)
            if await self._ladder_set_model(turn.model):
                self._ladder = turn
                log_event("engine", "claude_ladder_turn_started", tab_id=self.tab_id, model=turn.model)
        except Exception as exc:  # noqa: BLE001
            self._ladder = None
            log_event("engine", "claude_ladder_start_failed", tab_id=self.tab_id, error=str(exc)[:200])

    def _ladder_observe(self, wire: dict[str, Any] | None) -> None:
        turn = self._ladder
        if turn is not None and turn.active and wire is not None:
            turn.observe(wire)

    def _ladder_hold(self, wire: dict[str, Any] | None) -> bool:
        """True: this assistant message waits for the verdict (it is kept, not sent)."""
        turn = self._ladder
        if turn is None or not turn.active or turn.is_top or wire is None or wire.get("type") == "result":
            return False
        if not turn.should_hold(wire):
            return False
        turn.held.append({"message": wire})
        return True

    async def _ladder_flush(self, turn: claude_ladder.LadderTurn) -> None:
        """Sends what was held back, through the same steps a message normally takes."""
        held, turn.held = turn.held, []
        for entry in held:
            wire = _strip_no_update_from_wire(entry["message"], self.tab_id)
            if wire is not None:
                wire = await self._translate_wire_visible_text(wire)
            if wire is None:
                continue
            await self.send({"type": "sdk_message", "message": wire, "isVoice": self.turn_is_voice})
            self.real_user_turn_answered = True
            if claude_ladder.visible_text(wire):
                self.last_visible_output_at = time.monotonic()
                self.turn_saw_any_visible_text = True
                self.consecutive_narration_count = 0
                if self.pending_is_real_user:
                    self._turn_last_visible_text = claude_ladder.visible_text(wire)

    async def _ladder_on_result(self, message: Any) -> bool:
        """The turn's ResultMessage. True: it is swallowed (the attempt was rejected, the next rung has been
        started); False: handle it as usual (the held replies have been sent)."""
        turn = self._ladder
        if turn is None or not turn.active:
            return False
        if (self.hang_interrupt_result_pending or self.ignore_next_result_recovery
                or self.forced_compaction_result_pending or self.clear_tab_result_pending):
            return False
        try:
            usage = getattr(message, "usage", None)
            log_event("engine", "claude_ladder_rung_done", tab_id=self.tab_id, model=turn.model,
                      is_error=bool(getattr(message, "is_error", False)), usage=usage if isinstance(usage, dict) else None)
            candidate = claude_ladder.candidate_text(turn, getattr(message, "result", None))
            is_error = bool(getattr(message, "is_error", False))
            action, note = "done", ""
            if claude_ladder.needs_verdict(turn, candidate, is_error):
                from app.small_model_engine import verify_claude_attempt

                action, note = await verify_claude_attempt(turn.user_text, candidate, list(turn.trace), not turn.is_top, self.tab_id)
            if action == "up" and not turn.is_top:
                turn.next_rung(note)
                if await self._ladder_set_model(turn.model):
                    log_event("engine", "claude_ladder_escalated", tab_id=self.tab_id, to=turn.model, note=note[:200])
                    self._ladder_queue_continuation(turn, note)
                    return True
                turn.rung -= 1  # could not switch: the attempt stands
        except Exception as exc:  # noqa: BLE001 -- the reply must still go out
            log_event("engine", "claude_ladder_result_failed", tab_id=self.tab_id, error=str(exc)[:200])
        turn.active = False
        await self._ladder_flush(turn)
        return False

    def _ladder_queue_continuation(self, turn: claude_ladder.LadderTurn, note: str) -> None:
        text = _SYNTHETIC_TURN_MARKER + claude_ladder.continuation_note(turn, note)
        self.queue.insert(0, {
            "message": {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                        "parent_tool_use_id": None},
            "is_voice": self.turn_is_voice,
            "memory_advice_for": None,
            "ladder_continuation": True,
        })
        self._queue_event.set()

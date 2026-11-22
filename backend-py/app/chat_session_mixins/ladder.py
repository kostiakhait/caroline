from __future__ import annotations

import asyncio
import os

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header
from app import claude_ladder, rung_advice


class LadderMixin:
    """The model ladder of a Claude tab (app/claude_ladder.py): a turn starts on Haiku, or on the step the start-rung
    advice picks (app/rung_advice.py), an outside verifier decides whether the attempt finished the task, and if not
    the same session goes on with the next model. For Codex the same advice picks the turn's reasoning effort.
    Hooks: _rung_advice_begin and _ladder_before_yield (_input_stream), _ladder_observe / _ladder_hold (the message
    loop, per wire message), _ladder_on_result (the loop, on a ResultMessage). Every failure falls back to "no ladder
    for this turn"."""

    _ladder: claude_ladder.LadderTurn | None = None
    _ladder_model: str | None = None     # the model the live client is set to (None: the CLI's own default)
    _ladder_last_result_at: float | None = None   # monotonic time of the last turn result: the cache's last use
    _ladder_raised_calls: int = 0         # calls made on raised rungs since the session last stood on the bottom one
    _codex_effort: str | None = None     # the effort the live Codex thread was last given (None: Codex's own)
    _codex_turn: dict[str, Any] | None = None     # a real user's Codex turn waiting for its result (rung history)
    _reaction_task: "asyncio.Future[Any] | None" = None     # turn.reaction.score of the latest real message (submit())

    def _ladder_reset(self) -> None:
        self._ladder = None
        self._ladder_model = None
        self._ladder_last_result_at = None
        self._ladder_raised_calls = 0
        self._codex_effort = None
        self._codex_turn = None

    def _rung_advice_begin(self, item: dict[str, Any]) -> None:
        """Starts the start-rung advice for a real user's message as soon as _input_stream takes it, so it runs
        alongside the memory advisor; _ladder_before_yield waits for it. Internal notes and the ladder's own
        continuations are not advised."""
        try:
            if item.get("ladder_continuation") or not item.get("memory_advice_for"):
                return
            kind = getattr(self, "engine_kind", "")
            if kind == "claude" and self._ladder_active():
                engine, ladder = "claude", rung_advice.CLAUDE_LADDER
            elif kind == "openai" and os.environ.get("CAROLINE_NO_LADDER") != "1":
                engine, ladder = "codex", rung_advice.CODEX_EFFORTS
            else:
                return
            request = claude_ladder.user_text_of(item["message"]) or item["memory_advice_for"]
            item["rung_advice_task"] = asyncio.ensure_future(rung_advice.advise(
                engine, ladder, request, self._ladder_recent_dialogue(), self.workspace_dir, self.tab_id,
                self._reaction_task,
            ))
        except Exception as exc:  # noqa: BLE001 -- no advice: the turn starts where it always did
            log_event("engine", "turn_rung_advice_start_failed", tab_id=self.tab_id, error=str(exc)[:200])

    async def _rung_advice_result(self, item: dict[str, Any]) -> tuple[int | None, str]:
        task = item.pop("rung_advice_task", None)
        if task is None:
            return None, ""
        try:
            return await task
        except Exception:  # noqa: BLE001 -- advise() never raises; a cancelled task is no advice
            return None, ""

    async def _codex_effort_before_yield(self, item: dict[str, Any]) -> None:
        """Codex: the advised step of rung_advice.CODEX_EFFORTS goes into this turn's turn/start (CodexEngine reads
        "codex_effort" off the message). Codex keeps an effort for the thread's later turns, so a turn on step 0
        after a raised one is given step 0 again."""
        rung, why = await self._rung_advice_result(item)
        step = rung or 0
        effort = rung_advice.CODEX_EFFORTS[step][0]
        sent = step > 0 or self._codex_effort not in (None, effort)
        if sent:
            item["message"]["codex_effort"] = effort
            self._codex_effort = effort
        if item.get("memory_advice_for"):
            self._codex_turn = {"request": claude_ladder.user_text_of(item["message"]) or item["memory_advice_for"], "rung": step}
        log_event("engine", "codex_turn_effort", tab_id=self.tab_id, effort=effort if sent else None,
                  advised_rung=rung, why=why[:300])

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
        if model == claude_ladder.RUNGS[0]:
            self._ladder_raised_calls = 0
        return True

    async def _ladder_before_yield(self, item: dict[str, Any]) -> None:
        """Called by _input_stream for each queued message just before the SDK gets it."""
        try:
            if item.get("ladder_continuation"):
                return  # the next rung of the turn in progress: the model was set when the verdict came
            if self._ladder is not None and self._ladder.active and self._ladder_active():
                # A message arrived while the turn is still being judged: it joins that turn. Starting a new one here
                # would drop the replies held for the verdict.
                self._ladder.user_text = (self._ladder.user_text + "\n" + (claude_ladder.user_text_of(item["message"]) or "")).strip()
                task = item.pop("rung_advice_task", None)
                if task is not None:
                    task.cancel()
                return
            self._ladder = None
            if getattr(self, "engine_kind", "") == "openai":
                await self._codex_effort_before_yield(item)
                return
            if not self._ladder_active():
                # A model pinned in Settings applies from this message on, also to a client the ladder had moved.
                pinned = get_model_override(self.workspace_dir, "claude")
                if pinned and getattr(self, "engine_kind", "") == "claude" and self._ladder_model not in (None, pinned):
                    await self._ladder_set_model(pinned)
                return
            text = claude_ladder.user_text_of(item["message"]) or item.get("memory_advice_for") or ""
            rung, why = await self._rung_advice_result(item)
            real_user = bool(item.get("memory_advice_for"))
            rung = rung or 0
            current = claude_ladder.rung_index(self._ladder_model)
            if current is not None and rung < current:
                last = self._ladder_last_result_at
                cold = last is None or time.monotonic() - last > claude_ladder.CACHE_TTL_S
                if not claude_ladder.may_descend(cold, real_user, self._ladder_raised_calls):
                    log_event("engine", "claude_ladder_kept_rung", tab_id=self.tab_id, kept=claude_ladder.RUNGS[current],
                              wanted=claude_ladder.RUNGS[rung], real_user=real_user)
                    rung = current
            turn = claude_ladder.LadderTurn(user_text=text, rung=rung, start_rung=rung, real_user=real_user)
            if await self._ladder_set_model(turn.model):
                self._ladder = turn
                log_event("engine", "claude_ladder_turn_started", tab_id=self.tab_id, model=turn.model,
                          advised_rung=rung, why=why[:300])
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
        fake = (self.hang_interrupt_result_pending or self.ignore_next_result_recovery
                or self.forced_compaction_result_pending or self.clear_tab_result_pending)
        if self._codex_turn is not None and not fake:
            codex_turn, self._codex_turn = self._codex_turn, None
            rung_advice.record(self.workspace_dir, self.tab_id, "codex", codex_turn["request"], rung_advice.CODEX_EFFORTS,
                               codex_turn["rung"], codex_turn["rung"])
        turn = self._ladder
        if not fake:
            self._ladder_last_result_at = time.monotonic()
        if turn is None or not turn.active or fake:
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

                action, note = await verify_claude_attempt(
                    turn.user_text, candidate, list(turn.trace), not turn.is_top, self.tab_id, self._ladder_recent_dialogue(),
                )
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
        if turn.rung > 0:
            self._ladder_raised_calls += turn.calls
        if turn.real_user:
            rung_advice.record(self.workspace_dir, self.tab_id, "claude", turn.user_text, rung_advice.CLAUDE_LADDER,
                               turn.start_rung, turn.rung, turn.escalations)
        await self._ladder_flush(turn)
        return False

    def _ladder_recent_dialogue(self) -> str:
        """The exact record of the last messages (app/dialogue_log.py), for the verifier: what was done and shown earlier."""
        try:
            from app import dialogue_log

            entries = dialogue_log.select(dialogue_log.read_entries(self.workspace_dir, self.tab_id), limit=8)
            text = dialogue_log.render(entries)
            return "" if text == "(no messages match)" else text[-5000:]
        except Exception:  # noqa: BLE001
            return ""

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

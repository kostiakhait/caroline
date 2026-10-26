from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class StopControlMixin:
    def stop(self) -> None:
        # Invalidates any narration comment already mid-generation for the
        # turn being stopped -- see _narration_stop_epoch's own comment.
        # Unconditional and first: both the early-return (between turns) and
        # the main path below must invalidate it the same way.
        self._narration_stop_epoch += 1
        # An explicit Stop also drops a turn waiting to be replayed.
        self._cancel_memory_replay()
        # Latched before the turn_pending check below: between turns nothing is
        # pending, yet the post-turn check is what re-arms the loop, so Stop must
        # still halt it. Cleared only by the next real user message.
        self._post_turn_checks_halted = True
        # A request queued behind a proactive turn (see small_model_queued_real_turns)
        # must not run right after an explicit Stop -- that's the opposite of what Stop means.
        self.small_model_queued_real_turns = []
        # Bug fix (2026-10-09), confirmed live ("дважды жал стоп -- нихуя не
        # остановилось"): input already queued for the engine but not yet
        # taken by it (a nudge, a reminder, a follow-up, an earlier message)
        # survived Stop and was fed to the fresh engine the moment it came
        # back up. Everything queued before the click goes; what the user
        # sends AFTER it is a new instruction and is kept.
        dropped_inputs = len(self.queue)
        self.queue.clear()
        self.compaction_queued_turns = []
        # The model learns about the Stop with the user's next message (see
        # submit()), never through a turn of its own.
        self._stop_note_pending = True
        # An explicit Stop must also cancel any pending "resumed question needs a visible answer" re-ask,
        # or the abandoned turn would start again on its own.
        self.resumed_unanswered_question = None
        # An explicit user Stop (this method) is a separate event from a permission-guard
        # stop (_on_permission_block_checked, which calls this SAME method) -- clear
        # whatever the guard was holding too, so stale state from before the click can't
        # wrongly block the user's own next message. The next real message re-evaluates
        # this from scratch (see _check_permission_guard's own submit()-time call site).
        self.pending_unapproved_action = None
        # Bug fix (2026-10-09), confirmed live: these two timers used to be
        # cleared only at the very end of this method, AFTER the "nothing is
        # pending" early return just below. A tab stuck on a usage cap spends
        # almost all of its time BETWEEN turns -- each retry turn is rejected
        # within seconds, then 90s of waiting for the next one -- so Stop
        # nearly always landed between turns, returned early, and the retry
        # loop kept re-submitting every 90s as if the user had never stepped
        # in ("я жал стоп, но состояние не сбросилось"). Stop is the absolute
        # switch for this tab whether or not a turn happens to be in flight at
        # the instant of the click: every background recovery attempt is
        # silenced first, unconditionally.
        had_retry = bool(self.api_retry_timer or self.one_shot_followup_timer)
        self._clear_api_retry_timer()
        self._clear_one_shot_followup_timer()
        # ...and the state those retries were holding goes with them: with
        # nothing left to retry, "limited"/"billing_blocked" would otherwise
        # sit on the status bar forever. The next message the user sends
        # simply tries again and reports whatever is true then.
        if self.conn_state.get("kind") in STOP_RESETS_CONN_STATES:
            self._set_conn_state("connected")
        if not self.turn_pending:
            # Bug fix (2026-10-09), per explicit instruction: "основная модель
            # и все, что ей запущено из инструментов должно полностью
            # прерываться" -- also when the click lands between two turns.
            # No turn is in flight then, but what earlier turns launched may
            # well be: a tool call running as a background operation, a
            # subagent or a background shell inside the engine's own process.
            # Each of those wakes the model up again when it finishes, which
            # from the user's side is "I pressed Stop and she kept going".
            cancelled = REGISTRY.cancel_for_tab(self.tab_id, keep_tools=STOP_KEEPS_TOOLS)
            # Stop is the absolute kill switch: the engine's whole process
            # tree goes, unconditionally -- no asking a registry whether an
            # agent is "supposed" to be running. Whatever earlier turns left
            # behind inside the engine (subagents, background shells, anything
            # they launched) dies with it. The run loop starts a fresh, idle
            # engine afterwards; _stop_killed_idle_engine tells it that this
            # was the user's doing and that no turn is to be started over it.
            killed = 0
            if self.client and not self.small_model_active:
                self._stop_killed_idle_engine = True
                killed = self._kill_engine_process_tree("Stop between turns")
                asyncio.create_task(self._force_stop_client())
            log_event(
                "engine", "user_stop_between_turns", tab_id=self.tab_id, cancelled_retry=had_retry,
                cancelled_operations=cancelled, killed_processes=killed,
            )
            return
        log_event("engine", "user_stop", tab_id=self.tab_id, cancelled_retry=had_retry)
        self.user_stop_requested = True
        if self.small_model_active:
            # No SDK client/query() is involved in this path at all -- the
            # only thing to stop is the background task running
            # resolve_agentic() (in a worker thread) and, via REGISTRY
            # below, any tool call it already dispatched.
            if self._small_model_task is not None:
                self._small_model_task.cancel()
        elif self.client:
            # Bug fix (2026-09-15), per explicit instruction: "Кнопка стоп
            # это абсолютный рубильник... Сразу по нажатии" -- Stop must
            # stop EVERYTHING in this tab (dialogue, tasks, agents)
            # immediately on click, not best-effort or after a grace
            # period. The previous design (2026-09-14) tried a soft
            # interrupt() first and only escalated to a hard kill after
            # STOP_ESCALATION_GRACE_S (5s) -- confirmed live, twice now,
            # that soft interrupt() alone routinely doesn't work, so that
            # 5s was just a guaranteed delay before the user got real
            # control back, not a real chance for the polite path to
            # succeed. Goes straight for the hard kill now, with no grace
            # period: _force_stop_client (fires interrupt() AND
            # disconnect() concurrently, immediately, and falls back to a
            # direct OS-process kill if disconnect() itself throws -- see
            # its own docstring for the SDK bug that makes that fallback
            # necessary).
            # The whole process tree, right now and synchronously -- before
            # anything asynchronous below even gets scheduled.
            self._kill_engine_process_tree("Stop during a turn")
            asyncio.create_task(self._force_stop_client())
        # Idle at once: the run loop only notices the dead engine when its
        # stream breaks, which was seen to take 20-55 s; the status bar and
        # the next message must not wait for that.
        self.turn_pending = False
        log_event("engine", "user_stop_dropped_queued_input", tab_id=self.tab_id, count=dropped_inputs)
        # Bug fix (2026-09-10): confirmed live -- client.interrupt() alone
        # only stops the model's own generation stream. A tool call that
        # already crossed dispatch()'s fast-path window (app/operations.py)
        # became a detached background asyncio.Task in the process-wide
        # REGISTRY, entirely decoupled from this turn/session -- Stop never
        # reached it, so it kept running to completion regardless. This
        # cancels exactly THIS tab's own still-running background
        # operations alongside the interrupt.
        cancelled = REGISTRY.cancel_for_tab(self.tab_id, keep_tools=STOP_KEEPS_TOOLS)
        if cancelled:
            log_event("engine", "user_stop_cancelled_operations", tab_id=self.tab_id, count=cancelled)
    def _reset_after_stop(self) -> None:
        """The run loop's side of a Stop, once the killed engine's stream has
        broken. A message the user sent AFTER the click is already queued
        and is a new instruction: it keeps its turn and runs on the fresh
        engine. Otherwise the tab is simply idle."""
        self._ladder_reset()
        self._dr_reset()
        self._inflight_reset()
        if self.queue:
            log_event("engine", "after_stop_keeping_new_user_input", tab_id=self.tab_id, count=len(self.queue))
            return
        self.turn_pending = False
        self.pending_user_text = None
        self.pending_attachments = []
    def _stopped_by_user(self, what: str, reason: str) -> bool:
        """True after an explicit Stop, until the user's next real message
        (the same latch that halts post-turn checks). A retry turn that was
        already in flight at the moment of the click can still deliver its
        "limit" reply a moment later; without this, that late reply re-armed
        the very retry loop Stop had just cancelled."""
        if self._post_turn_checks_halted:
            log_event("engine", f"{what}_not_scheduled_after_stop", tab_id=self.tab_id, reason=reason)
            return True
        return False

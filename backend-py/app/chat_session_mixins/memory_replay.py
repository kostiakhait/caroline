from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class MemoryReplayMixin:
    def _cancel_memory_replay(self) -> None:
        self._memory_replay = None
        if self._memory_replay_timer:
            self._memory_replay_timer.cancel()
            self._memory_replay_timer = None
    def _on_memory_upstream_no_funds(self, reason: str) -> None:
        """The memory service could not run because ITS provider (OpenRouter,
        our own cost) is out of money -- nothing the user's wallet can fix.
        Per the agreed rule (docs/MICROAGENTS_PLAN.md, section 4): the turn is
        reset to its start. It is stopped exactly as the Stop button stops
        it, the status bar says why, and once the provider answers again the
        user's message that began the turn is submitted anew. What the turn
        already did (a letter sent) is not undone.

        Called from inside a tool call of the very turn being stopped, so the
        stop itself is deferred to the loop."""
        turn = memory_turns.current_turn(self.tab_id)
        log_event("engine", "memory_upstream_no_funds", tab_id=self.tab_id, reason=reason, replayable=bool(turn and turn.get("text")))
        mark_funds_exhausted(reason)
        replay = dict(turn) if turn and turn.get("text") else None

        def _stop_and_wait() -> None:
            if self.ended:
                return
            self.stop()                      # also clears any earlier replay
            self._memory_replay = replay
            if replay:
                self._arm_memory_replay_timer()
            asyncio.create_task(self._publish_status())

        asyncio.get_event_loop().call_soon(_stop_and_wait)
    def _arm_memory_replay_timer(self) -> None:
        """Flat interval, forever, like every other retry here."""
        def _fire() -> None:
            self._memory_replay_timer = None
            if not self.ended and self._memory_replay:
                asyncio.create_task(self._check_memory_replay())

        self._memory_replay_timer = asyncio.get_event_loop().call_later(API_RETRY_INTERVAL_MS / 1000, _fire)
    async def _check_memory_replay(self) -> None:
        """One cheap call that goes through the same provider: sw_api clears
        its funds-exhausted mark on any call that succeeds and sets it again
        on a 402, so the mark itself is the answer."""
        from app.plugins.voice_api import detect_language
        await detect_language("ping")
        replay = self._memory_replay
        if not replay or self.ended:
            return
        if get_funds_exhausted_reason():
            self._arm_memory_replay_timer()
            return
        log_event("engine", "memory_replay_resubmitting", tab_id=self.tab_id, turn_id=replay["id"])
        self._memory_replay = None
        self.submit(replay["text"], replay["attachments"], is_real_user=True, is_voice=replay["is_voice"])

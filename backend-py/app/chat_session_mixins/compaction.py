from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class CompactionMixin:
    async def _pre_compact_hook(self, hook_input: Any, tool_use_id: Any, context: Any) -> dict[str, Any]:
        """Fires just before Claude's own compaction summarises older turns
        -- either its own native auto-compaction, or our forced "/compact"
        (see FORCED_COMPACTION_HOURLY_MS; trigger is "manual" for that one,
        live-confirmed).

        Bug fix (2026-09-20), confirmed live: this used to copy the ENTIRE
        transcript into workspace/dehydrated/ on every compaction and point
        the tab's continuity pointer at the copy. Compaction never touches
        the live transcript, though -- it is append-only, and after 10 days
        of hourly compactions tab 1's still held every record back to its
        first day (1,325 compact_boundary markers inside it), and the newest
        archive was a byte-for-byte prefix of it. So the copies preserved
        nothing the live file didn't already have, while costing a
        hundreds-of-MB copy each time: 4,484 of them, 892 GB, mostly from a
        compaction loop (see _note_compaction_hit_limit). The continuity
        pointer they set was wrong too -- it tells the model "an earlier
        session was abandoned after an internal error", which is simply
        false after an ordinary compaction. Nothing is copied now; the
        24h-dialogue file (recent_dialogue_history_instruction) is the
        recall path, and it reads the live file's tail. Observation only.

        Bug fix (2026-09-28), confirmed live from a real incident (tab 1,
        18:58 EDT): forced_compaction_result_pending -- the flag that
        suppresses the compaction round-trip's own AssistantMessage/
        ResultMessage traffic from ever reaching the client as a chat
        bubble -- was ONLY ever set by our own forced_compaction() before
        it pushes "/compact" (see that method). The CLI's OWN native
        auto-compaction (context grew too large mid-turn, no "/compact"
        from us) fires this exact same hook but never went through that
        method, so nothing suppressed it -- the model's raw <analysis>/
        <summary> compaction recap (see compact_continuation_is_genuine_
        instruction, which correctly tells the model to WRITE this, but
        says nothing about who should ever SEE it) sailed straight through
        to the user as an ordinary reply. Setting it here too, for BOTH
        triggers, makes suppression unconditional on which path caused
        the compaction -- a no-op when forced_compaction() already set it.
        See _handle_failure's own matching fix: this flag now gets cleared
        on ANY stream teardown, not just an engine switch, specifically so
        a crash mid-compaction (confirmed live: this exact incident ended
        in a ClosedResourceError right after the leaked text, before any
        ResultMessage could clear it) can never leave it stuck True and
        silently mute every later reply."""
        trigger = hook_input.get("trigger") if isinstance(hook_input, dict) else None
        log_event(
            "engine", "pre_compact_observed", tab_id=self.tab_id, trigger=trigger,
            was_already_suppressed=self.forced_compaction_result_pending,
        )
        self.forced_compaction_result_pending = True
        return {}
    def _current_session_file_size(self) -> int | None:
        """Bytes on disk for this tab's currently-resumed session transcript,
        or None if there's nothing resolvable yet (no session id, or the
        file genuinely isn't there). No longer the growth-trigger's own
        decision signal (2026-09-15 -- see tokens_at_last_forced_
        compaction's own __init__ comment for why file bytes were wrong for
        that) -- kept only for the session_size_bytes log field, which is
        still useful context even though it's not what drives the trigger."""
        if not self.last_saved_session_id:
            return None
        try:
            path = claude_project_dir(self.workspace_dir) / f"{self.last_saved_session_id}.jsonl"
            return path.stat().st_size
        except OSError:
            return None
    def _push_internal_command(self, text: str) -> None:
        """Queues a raw CLI command (e.g. "/compact") the same way a human
        typing it would send it -- bypasses submit()/_push_message entirely
        (no "[Sent: ...]" timestamp line, no attachments, no turn_pending/
        pending_user_text bookkeeping) since this isn't a conversational
        turn at all. Live-confirmed this exact shape (content as a
        single-block list, no extra text) is recognized as a slash command
        through THIS engine's own connect()+generator wiring, not just the
        SDK's separate query(str) convenience path."""
        self.queue.append({
            "message": {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}, "parent_tool_use_id": None},
            "is_voice": False,
        })
        self._queue_event.set()
    def _check_forced_compaction(self) -> None:
        """See FORCED_COMPACTION_HOURLY_MS's own comment for why this
        exists at all (native auto-compaction confirmed never firing on its
        own). Four triggers, checked in priority order: needs_post_limit_
        compaction_check (per explicit instruction, 2026-09-15 -- see its
        own __init__ comment; deliberately bypasses the cooldown gate
        below, since the whole point is retrying what a usage-limit hit
        just interrupted, not waiting for the next regular cycle), a
        pending startup compaction (main.py sets needs_startup_compaction
        once per tab per process), the hourly clock, or real context-token
        growth past FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD since the
        last forced compaction. Never runs while a real turn is in flight or the
        connection isn't fully settled -- this is maintenance, not
        something to inject into or race with actual work."""
        if self.engine_kind != "claude":
            return  # Codex owns its own compaction; this machinery edits Claude transcripts
        if self.ended or self.turn_pending or self.forced_compaction_result_pending:
            return
        if self.conn_state.get("kind") != "connected":
            return
        if not self.last_saved_session_id:
            return
        now = time.monotonic()
        # Right after a compaction that itself hit the usage cap: wait, no
        # matter which trigger is armed -- INCLUDING needs_post_limit_
        # compaction_check, which used to bypass every gate and is exactly
        # what turned one failed compaction into thousands (see
        # FORCED_COMPACTION_LIMIT_PAUSE_MS). The armed flag simply stays
        # armed until the pause ends.
        if now < self.forced_compaction_blocked_until:
            return

        if self.tokens_at_last_forced_compaction is not None and self.tokens_at_last_forced_compaction >= FALLBACK_COMPACTION_THRESHOLD_TOKENS:
            # Highest priority, and deliberately ahead of the MIN_INTERVAL
            # cooldown below: this baseline was JUST refreshed by a
            # compact_boundary_observed (native auto-compaction never goes
            # through last_forced_compaction_at at all, so that cooldown
            # must not gate this). Clears itself: _run_fallback_compaction
            # resets this to None once it's dispatched, same as any
            # pending-result flag elsewhere in this method.
            reason = "fallback_still_too_large"
        elif self.needs_post_limit_compaction_check:
            self.needs_post_limit_compaction_check = False
            reason = "post_limit"
        elif self.last_forced_compaction_at is not None and now - self.last_forced_compaction_at < FORCED_COMPACTION_MIN_INTERVAL_MS / 1000:
            return
        elif self.needs_startup_compaction:
            reason = "startup"
        elif self.last_forced_compaction_at is None or now - self.last_forced_compaction_at >= FORCED_COMPACTION_HOURLY_MS / 1000:
            reason = "hourly"
        else:
            reason = None
            if self.tokens_at_last_forced_compaction is not None and self.last_known_context_tokens is not None:
                grown = self.last_known_context_tokens - self.tokens_at_last_forced_compaction
                if grown >= FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD:
                    reason = "growth"

        if reason is None:
            return

        # Nothing to compact -> don't (except "growth", which by definition
        # already measured real growth). Measured live: every hourly/startup
        # compaction on an idle tab ran with 3.5K-7K tokens in context and
        # came out the same size, yet each one still cost a full transcript
        # scan/copy. The size comes from the live turns' own usage reports
        # when this process has seen any, otherwise from the END of the
        # transcript itself (read_last_context_tokens) -- a fresh process
        # has no usage report yet, which is precisely when the startup
        # compaction fires. Unknown (None) keeps the old behavior: compact.
        if reason != "growth":
            context_tokens = self.last_known_context_tokens
            if context_tokens is None:
                try:
                    path = claude_project_dir(self.workspace_dir) / f"{self.last_saved_session_id}.jsonl"
                    context_tokens = read_last_context_tokens(path)
                    if context_tokens is not None:
                        self.last_known_context_tokens = context_tokens
                except Exception as exc:
                    log_event("engine", "forced_compaction_context_probe_failed", tab_id=self.tab_id, error=str(exc))
            if context_tokens is not None and context_tokens < FORCED_COMPACTION_MIN_CONTEXT_TOKENS:
                log_event(
                    "engine", "forced_compaction_skipped_small_context", tab_id=self.tab_id, reason=reason,
                    context_tokens=context_tokens, threshold=FORCED_COMPACTION_MIN_CONTEXT_TOKENS,
                )
                # Treated as "checked": no re-evaluation until the next
                # hourly slot (or real growth), not on every 5 s tick.
                self.needs_startup_compaction = False
                self.last_forced_compaction_at = now
                return

        # Save first (per explicit instruction, 2026-10-10): one service turn
        # asking the model to save what it learned; the compaction itself
        # runs on a later tick, once that turn is over (turn_pending gates
        # this whole method). Not for "startup" (nothing was learned in this
        # process yet) nor "post_limit" (the turn would hit the cap again).
        if reason in ("hourly", "growth") and memory_nudge.has_unsaved_work(self.tab_id) and not self._memory_flushed_for_compaction:
            self._memory_flushed_for_compaction = True
            log_event("engine", "memory_flush_before_compaction", tab_id=self.tab_id, reason=reason)
            self.inject_proactive(MEMORY_FLUSH_BEFORE_COMPACTION_TEXT, suppress_narration=True)
            return
        self._memory_flushed_for_compaction = False

        self.needs_startup_compaction = False
        self.last_forced_compaction_at = now
        self.size_at_last_forced_compaction = self._current_session_file_size() or 0
        log_event(
            "engine", "forced_compaction_triggered", tab_id=self.tab_id, reason=reason,
            session_size_bytes=self.size_at_last_forced_compaction,
            context_tokens_at_trigger=self.last_known_context_tokens,
            context_tokens_baseline=self.tokens_at_last_forced_compaction,
        )
        # Per explicit instruction (2026-09-18): this flag flipping is the
        # ONLY thing that makes compaction visible on the status bar (see
        # _compute_public_status) -- nothing else republishes status for
        # it, since compaction deliberately never touches turn_pending
        # (whose own setter is the usual auto-publish trigger).
        asyncio.create_task(self._publish_status())
        if reason == "fallback_still_too_large":
            # Not a "/compact" -- that's exactly what already ran and
            # wasn't enough. See _run_fallback_compaction.
            asyncio.create_task(self._run_fallback_compaction())
            return
        self.forced_compaction_result_pending = True
        self._push_internal_command("/compact")
    async def _run_fallback_compaction(self) -> None:
        """The local, no-model second pass (app/compaction.py) for when
        native/forced compaction already ran and tokens_at_last_forced_
        compaction is still at/above FALLBACK_COMPACTION_THRESHOLD_TOKENS
        (see that constant's own comment). Forks the live session, ages out
        everything but the most recent ~50KB of real content, points the tab
        at the fork, and disconnects -- the run loop's own reconnect path
        (_resolve_resume_session_id) picks the fork up the same way it picks
        up any other saved session id, exactly like the existing hang-
        escalation disconnect at _check_hang does. Never raises -- this is
        best-effort maintenance, same guarantee as compact_session_if_due
        itself."""
        session_id = self.last_saved_session_id
        if not session_id:
            self.tokens_at_last_forced_compaction = None
            return
        try:
            result = await compact_session_if_due(self.workspace_dir, session_id, None, force=True)
        except Exception as exc:
            log_event("engine", "fallback_compaction_failed", tab_id=self.tab_id, error=str(exc))
            self.tokens_at_last_forced_compaction = None
            return
        if result is None:
            self.tokens_at_last_forced_compaction = None
            return
        save_tab_session_id(self.workspace_dir, self.tab_id, result.new_session_id, self.engine_kind)
        self.last_saved_session_id = result.new_session_id
        # The fork has no known token count yet -- the next compact_boundary
        # or usage report establishes a fresh baseline; comparing against a
        # stale pre-fallback number here would be wrong.
        self.tokens_at_last_forced_compaction = None
        self.last_known_context_tokens = None
        log_event("engine", "fallback_compaction_done", tab_id=self.tab_id, old_session_id=session_id, new_session_id=result.new_session_id)
        try:
            if self.client:
                await self.client.disconnect()
        except Exception as exc:
            log_event("engine", "fallback_compaction_disconnect_failed", tab_id=self.tab_id, error=str(exc))
    def _note_compaction_hit_limit(self, limit_text: str) -> None:
        """A forced compaction just failed because the usage cap is in force.
        Per explicit instruction (2026-09-20), after a real outage: this used
        to be handled like a real turn hitting the cap -- the tab went
        "limited", and the error's own trailing ResultMessage flipped it back
        to "connected" within a millisecond, which is the exact edge that
        arms needs_post_limit_compaction_check (retry what the cap
        interrupted, no cooldown) -- so the next 5 s watchdog tick compacted
        again, failed again, and so on for as long as the cap lasted (1,464
        times in one hour), each attempt copying the whole transcript.
        Maintenance failing against a cap isn't a tab-level event at all: no
        state change, just a flat pause before the next attempt (see
        FORCED_COMPACTION_LIMIT_PAUSE_MS). The trailing ResultMessage is
        consumed by the normal forced-compaction path (forced_compaction_
        result_pending is still True) exactly as any other compaction
        result."""
        self.forced_compaction_blocked_until = time.monotonic() + FORCED_COMPACTION_LIMIT_PAUSE_MS / 1000
        log_event(
            "engine", "forced_compaction_hit_limit", tab_id=self.tab_id,
            pause_s=FORCED_COMPACTION_LIMIT_PAUSE_MS // 1000, message=limit_text[:200],
        )
    def _drain_funds_blocked_queue(self) -> None:
        """Called right after _check_funds_exhaustion_status notices
        sw_api.get_funds_exhausted_reason() has gone back to None --
        replays every submit() call that arrived while the upstream
        balance was exhausted, in the order they originally arrived,
        through the exact same submit() they'd have gone through
        immediately if nothing had been blocked at all. See submit()'s own
        funds-gate and funds_blocked_queued_turns' own __init__ comment for
        the incident this fixes. Same shape as _drain_compaction_queue just
        below, minus that one's compaction-specific reminder -- nothing
        here was mid-compaction, there is no "got cut off" risk to warn
        about."""
        if not self.funds_blocked_queued_turns:
            return
        queued = self.funds_blocked_queued_turns
        self.funds_blocked_queued_turns = []
        log_event("engine", "funds_blocked_queue_drained", tab_id=self.tab_id, count=len(queued))
        for item in queued:
            self.submit(item["text"], item["attachments"], item["is_real_user"], item["is_voice"], pending_text=item.get("pending_text"))
    def _drain_compaction_queue(self) -> None:
        """Called right after forced_compaction_result_pending flips back
        to False (both places: the normal ResultMessage consumption path,
        and _apply_conn_state's own post-usage-cap-limit recovery edge
        case) -- replays every submit() call that arrived while compaction
        was still in flight, in the order they originally arrived, through
        the exact same submit() they'd have gone through immediately if
        compaction hadn't been running at all. See submit()'s own guard
        and compaction_queued_turns' __init__ comment for the incident
        this fixes."""
        if not self.compaction_queued_turns:
            return
        queued = self.compaction_queued_turns
        self.compaction_queued_turns = []
        log_event("engine", "compaction_queue_drained", tab_id=self.tab_id, count=len(queued))
        had_real_user_turn = any(item["is_real_user"] for item in queued)
        for item in queued:
            self.submit(item["text"], item["attachments"], item["is_real_user"], item["is_voice"], pending_text=item.get("pending_text"))
        if had_real_user_turn:
            # Per explicit instruction (2026-09-18), after a real incident:
            # a real user message got queued behind compaction and the
            # reply that followed falsely claimed the message itself "got
            # cut off" -- context had just been summarized right as it
            # arrived. recent_dialogue_history_instruction (policies.py,
            # always in the system prompt, unaffected by compaction -- it
            # summarizes conversation turns, not the system prompt) already
            # covers this in general, but an explicit, in-the-moment
            # reminder right here is more reliable than trusting the model
            # to recall a general standing rule at exactly the one moment
            # it matters most. Queued AFTER the real message(s) above, not
            # merged into their own text (last_real_user_question/
            # pending_user_text must stay the user's actual clean words).
            lang = current_language_name(self.tab_id)
            self.submit(
                "[Internal: a forced compaction just finished right as the user's message above arrived -- your "
                "context just got summarized, so some detail may not obviously still be there. Before asking the "
                "user to re-explain anything or claiming their message was incomplete/cut off, check your recent "
                "dialogue history file (the path is in your system prompt, refreshed independently of compaction) "
                f"first -- it still has the real recent back-and-forth. Then answer their actual message normally. "
                f"Reply in {lang}. Don't mention this note itself.]",
                [], False, False,
            )

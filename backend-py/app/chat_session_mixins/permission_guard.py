from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class PermissionGuardMixin:
    def _on_permission_block_checked(self) -> str | None:
        """Per explicit instruction (2026-10-11): registered with
        session_context's register_permission_blocker, called by
        app/operations.py's dispatch() right before EVERY tool call in
        either engine, for this tab. A truthy pending_unapproved_action
        means a tool call was just attempted while an unanswered yes/no
        permission question, or an un-approved communication/financial
        action, is outstanding (see _check_permission_guard) -- stop the
        WHOLE turn, not just this one call, per explicit instruction ("Ход
        останавливаться").

        Bug fix (2026-10-12), confirmed live (a real incident: a draft
        submission was correctly held up, but the user was never told why --
        the explanation used to be queued to attach to the user's own NEXT
        real message, see the old _permission_note_pending mechanism; if
        they didn't happen to send one, it never reached them at all and
        the turn just looked dead -- "отвалилась молча"). stop() and the
        explanation are now both fired immediately, deferred one tick via
        call_soon (same pattern as _on_memory_upstream_no_funds's own
        _stop_and_wait): stop() must finish its own synchronous cleanup (in
        particular self.queue.clear()) before inject_proactive() queues the
        new note, or the note could be wiped out in the same tick it was
        added. inject_proactive() starts a real new turn, so the model gets
        a chance to actually tell the user why right away, not whenever
        they next happen to speak.

        One stop per violation: the flag is consumed (cleared) here, not
        left set to re-trigger stop() again on every subsequent call.

        Escalation (2026-10-13), per explicit instruction ("если модель её не
        слушает, он должен сам спросить у пользователя, а ход полностью
        остановить"): the first block of a real-user episode still just tells
        the MODEL to ask and wait (inject_proactive(note_text) below, starting
        a fresh turn so it gets a chance to actually do that). Confirmed live
        the model can ignore that instruction and retry the same blocked
        action anyway on the very next turn -- permission_guard_consecutive_
        blocks (reset only by a genuine new real-user message, see submit())
        counts this. From the SECOND consecutive block onward, the guard stops
        asking the model and asks the user directly itself
        (_send_permission_guard_direct_ask) -- and critically does NOT
        inject_proactive() afterward, so no new turn starts and the model gets
        no further chance to retry until a real user message answers."""
        reason = self.pending_unapproved_action
        if reason:
            self.pending_unapproved_action = None
            self.permission_guard_consecutive_blocks += 1
            # Per explicit instruction (2026-10-13): a headless session (see
            # self.headless's own __init__ comment) has no delivery path for
            # _send_permission_guard_direct_ask -- it calls self.send(...), which
            # for a headless session reaches no chat window at all (confirmed:
            # this was already a dormant bug for the Ratatosk session, which
            # predates this flag -- a 2nd-consecutive block there silently asked
            # a question nobody could ever see or answer, with the turn stuck
            # fully stopped). Never escalate for a headless session; the ordinary
            # note-to-the-model branch below folds the yes/no question into the
            # model's own next real reply instead, which then flows out through
            # the normal buffer-and-relay path like any other answer.
            escalate = self.permission_guard_consecutive_blocks >= 2 and not self.headless
            log_event(
                "engine", "permission_guard_stopped_turn", tab_id=self.tab_id, reason=reason,
                consecutive=self.permission_guard_consecutive_blocks, escalated=escalate,
            )

            def _stop_and_explain() -> None:
                if self.ended:
                    return
                self.stop()
                # stop() unconditionally also queues the generic "the user pressed Stop"
                # note (self._stop_note_pending) -- wrong here (the user didn't click
                # anything) and would otherwise show up ALONGSIDE the permission note
                # below. The permission note already says everything that matters.
                self._stop_note_pending = False
                if escalate:
                    asyncio.ensure_future(self._send_permission_guard_direct_ask(reason))
                else:
                    self.inject_proactive(PERMISSION_NOTE_TEXT_TEMPLATE.format(reason=reason))

            asyncio.get_event_loop().call_soon(_stop_and_explain)
        return reason
    async def _send_permission_guard_direct_ask(self, reason: str) -> None:
        """The escalation path _on_permission_block_checked falls to once the
        model has already ignored one PERMISSION_NOTE_TEXT_TEMPLATE instruction
        and retried the same unapproved action anyway. Bypasses the model
        entirely and asks the real user directly -- same bypass-the-model,
        translate-then-send-as-wire pattern as deliver_relayed_reply.
        The caller does not inject_proactive() after this: no new turn starts,
        so there is nothing left to retry the blocked action until a genuine
        user message (submit() with is_real_user=True) answers it."""
        from app.plugins.voice_api import translate_text

        text = f"I need to stop and ask you directly: {reason} Please tell me yes or no before I continue."
        try:
            translated = await translate_text(
                text, current_language_name(self.tab_id), gender=get_persona_gender(self.workspace_dir),
                addressee_gender=get_owner_profile(self.workspace_dir).gender,
            )
        except Exception as exc:
            log_event("engine", "permission_guard_direct_ask_translate_failed", tab_id=self.tab_id, error=str(exc))
            translated = None
        if translated:
            text = translated
        log_event("engine", "permission_guard_asked_user_directly", tab_id=self.tab_id, reason=reason)
        self.last_visible_output_at = time.monotonic()
        wire = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}], "model": None, "stop_reason": None},
            "session_id": None, "parent_tool_use_id": None,
        }
        await self.send({"type": "sdk_message", "message": wire, "isVoice": self.turn_is_voice})
    async def _check_permission_guard(self, extra_context: str | None = None) -> None:
        """Per explicit instruction (2026-10-11): re-evaluates
        self.pending_unapproved_action from the recent dialogue, via the
        server-side turn.permission.guard microagent (reforce-microagents,
        same escalation ladder as reply.review -- deciding whether approval
        was actually given needs real judgment, not a keyword check).
        Called after every outbound assistant message (same 3 send sites
        reply.review already uses, same cadence -- no extra network cost
        beyond what that already pays), from submit() on every real user
        message (so a just-given "yes" clears the flag before the model's
        NEXT turn even starts, rather than wrongly blocking its first tool
        call on stale state), and from inject_proactive (see extra_context
        below).

        `extra_context` (bug fix, 2026-10-11, confirmed live): a proactive
        note (e.g. "[New Telegram message in the owner's account...]") is
        marked with _SYNTHETIC_TURN_MARKER and _usable_dialogue_lines
        -- what _gather_recent_dialogue_for_narration reads -- drops every
        synthetic line on purpose (so the narrator never reacts to internal
        bookkeeping). That is exactly right for the narrator, but it means
        the ONE piece of text that says whose account/identity a reply
        would go out under -- "your own account" vs "the owner's own
        correspondence" -- was invisible to this check too, confirmed live
        via a direct call against the real deployed agent: an owner-account
        note with no reply drafted yet came back needs_approval=False
        (nothing to flag YET, correctly, but also never re-checked once
        the model actually tries to reply) while a self-account note came
        back True (the model had nothing in the dialogue telling it this
        was Caroline's own identity, since that exact sentence had been
        stripped). inject_proactive passes the raw note text here so the
        guard sees it however it reads today's actual notifications tell
        the model whose account a reply would be under, same text the
        model itself is about to act on.

        Sets the reason string when something needs approval and does not
        have it yet; clears it (None) otherwise. Does NOT itself stop
        anything -- enforcement happens once, lazily, the next time a tool
        call is actually attempted (see _on_permission_block_checked,
        app/operations.py's dispatch()). Fail-open on any error/timeout:
        leaves pending_unapproved_action exactly as it was, same philosophy
        as translate_text/review_reply -- an infrastructure hiccup must not
        silently clear a real pending block, nor manufacture one that was
        never there.

        Bug fix (2026-10-15), confirmed live: a user's own CLARIFICATION of
        the SAME still-pending action ("я ж сказал ЕМУ пиши") used to reset
        permission_guard_consecutive_blocks to 0 just by virtue of being a
        new real-user message (see submit()'s own removed reset) -- the
        model then got a fresh, and unreliable, "ask nicely yourself" grace
        cycle for something it had already been told twice. Per explicit
        instruction ("почему мы делаем строковое сравнение вместо LLM-
        сравнения... бели маленькую модель и сравнивай"): sameness is now
        judged by turn.permission.guard itself (same_action_as_before),
        semantically, from the whole dialogue it already reads -- never a
        hardcoded string/slug comparison here, which a confirmed live
        wording drift (the SAME action described two different ways across
        two consecutive real calls) would have broken anyway. The counter
        only resets when the agent says this is a genuinely different
        action (or there is nothing pending at all)."""
        from app.plugins.voice_api import check_permission_guard

        dialogue = self._gather_recent_dialogue_for_permission_guard()
        if extra_context:
            note = " ".join(extra_context.split())
            dialogue = f"{dialogue}\n[Event just received, not yet acted on: {note}]" if dialogue else f"[Event just received, not yet acted on: {note}]"
        try:
            result = await check_permission_guard(
                self._narration_task(), dialogue, previous_block_reason=self.permission_guard_last_reason,
            )
        except Exception as exc:
            log_event("engine", "check_permission_guard_failed", tab_id=self.tab_id, error=str(exc))
            return
        if result is None:
            return
        if result["needs_approval"]:
            self.pending_unapproved_action = result["reason"]
            if not result["same_action_as_before"]:
                self.permission_guard_consecutive_blocks = 0
            self.permission_guard_last_reason = result["reason"]
        else:
            self.pending_unapproved_action = None
            self.permission_guard_consecutive_blocks = 0
            self.permission_guard_last_reason = None
    def _gather_recent_dialogue_for_permission_guard(self) -> str:
        """Bug fix (2026-10-12): _check_permission_guard used to reuse
        _gather_recent_dialogue_for_narration() wholesale -- see
        PERMISSION_GUARD_DIALOGUE_WINDOW's own comment for the false-
        positive this caused. This is the same shape (dual-engine handling,
        consecutive-repeat dedup) but with its own, much wider window and
        deliberately NO min_ts_ms floor -- every other caller of
        _read_recent_dialogue_lines already gets the unbounded default, and
        the guard belongs with them, not with the narrator's own
        current-turn-only restriction.

        Per explicit instruction (2026-10-13): confirmed live -- even this
        wider window still isn't enough on a high-traffic tab (60 lines
        consumed in well under an hour, scrolling out a real approval the
        user had given just ~3 hours earlier). Rather than just raising the
        number again (same failure mode on an even busier day), a session
        with approval_bridge_tab_id set (a headless background/reminder-task
        session -- see that field's own __init__ comment) also gets the
        BRIDGED tab's own recent_real_user_directives_text prepended: pure
        in-process, time-pruned, never touches disk, so it is immune to both
        traffic volume and context compaction by construction."""
        if self.small_model_active and self.small_model_live_dialogue:
            own = "\n".join(self.small_model_live_dialogue[-PERMISSION_GUARD_DIALOGUE_WINDOW:])
        else:
            lines = _read_recent_dialogue_lines(
                self.last_saved_session_id, self.tab_id, self.workspace_dir, PERMISSION_GUARD_DIALOGUE_WINDOW,
            )
            deduped: list[str] = []
            for line in lines:
                if not deduped or deduped[-1] != line:
                    deduped.append(line)
            own = "\n".join(deduped[-PERMISSION_GUARD_DIALOGUE_WINDOW:])
        if self.approval_bridge_tab_id:
            bridge = get_recent_real_user_directives(self.approval_bridge_tab_id)
            if bridge:
                return f"{bridge}\n{own}" if own else bridge
        return own
    async def _check_action_before_send(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """Registered with session_context's register_send_action_guard;
        awaited by app/operations.py's dispatch() right before a send-type
        tool call's real handler runs -- see that registration's own
        docstring, and ActionSummary.py (reforce-microagents) for the real
        incident this closes: a send choosing which account/identity to go
        out under is a decision made silently inside THIS call's own
        arguments, with nothing said about it in the visible dialogue
        beforehand, so the regular _check_permission_guard (text-only) has
        no way to ever see it in time.

        Summarizes this exact call into one plain sentence (turn.action.
        summary, cheap and judgment-free), then runs the SAME turn.
        permission.guard check _check_permission_guard already does
        elsewhere, with that sentence spliced in as extra_context -- so
        permissionGuard's own prompt/judgment is untouched, it just stops
        being blind to this one fact. Returns the block reason if one comes
        back, else None.

        Fail-open on a failed summary (never a hard dependency, same
        philosophy as every other microagent check here): proceeding
        without this extra visibility layer still leaves the regular,
        text-only guard check in place exactly as before this existed --
        strictly additive, never a new way to get stuck."""
        from app.plugins.voice_api import summarize_action

        try:
            summary = await summarize_action(tool_name, args)
        except Exception as exc:
            log_event("engine", "summarize_action_failed", tab_id=self.tab_id, tool=tool_name, error=str(exc))
            summary = None
        if not summary:
            return None
        log_event("engine", "action_summarized", tab_id=self.tab_id, tool=tool_name, summary=summary)
        await self._check_permission_guard(extra_context=f"About to happen, not yet done: {summary}")
        return self.pending_unapproved_action

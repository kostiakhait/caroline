from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header

# Per explicit special order (2026-10-19), the evolution-feedback loop: a
# reaction score below this magnitude is treated as "no real reaction" and
# never reaches evolution.backprop at all -- most real messages score very
# close to 0.0, and backprop is meaningless noise at that point. A tunable
# constant, not user-configurable for v1.
REACTION_SCORE_BACKPROP_THRESHOLD = 0.2


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
    def _dialogue_with_note(self, extra_context: str | None) -> str:
        """Factored out (2026-10-18) so _check_action_before_send can build
        the exact same dialogue-plus-note text _check_permission_guard
        feeds turn.permission.guard, for its OWN direct identify call (see
        that method's own docstring for why it needs to run identify
        itself, before deciding whether to call the guard at all)."""
        dialogue = self._gather_recent_dialogue_for_permission_guard()
        if not extra_context:
            return dialogue
        note = " ".join(extra_context.split())
        return f"{dialogue}\n[Event just received, not yet acted on: {note}]" if dialogue else f"[Event just received, not yet acted on: {note}]"

    async def _check_permission_guard(
        self, extra_context: str | None = None, facts: dict[str, Any] | None = None, action_id: str | None = None,
    ) -> None:
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
        action (or there is nothing pending at all).

        `facts` (2026-10-18, the action-boundary tracking system):
        _check_action_before_send already ran turn.permission.identify
        itself (it needs matched_action_id before deciding whether this
        call is even worth a guard call at all) and passes its own result
        straight through, so this method's own call to voice_api.
        check_permission_guard never repeats that same local model call a
        second time. None (every other call site) means check_
        permission_guard runs identify itself, same as always.

        `action_id` (2026-10-19, the evolution-feedback loop): passed
        straight through to check_permission_guard so a tournament round
        actually resolved by THIS call gets journaled against this same
        action (see app/evolution_journal.py) -- only _check_action_before_
        send supplies this (it already has the resolved action in hand);
        every other call site omits it and that round simply never
        attributes to an action (it may still attribute to the tab's
        current track, which is resolved here either way)."""
        from app.plugins.voice_api import check_permission_guard

        dialogue = self._dialogue_with_note(extra_context)
        if action_id is not None:
            action = self.action_registry.get(action_id)
            track_id = action.track_hint if action else None
        else:
            from app.memory_topics import get_current_topic
            track_id = get_current_topic(self.workspace_dir, self.tab_id)
        try:
            result = await check_permission_guard(
                self._narration_task(), dialogue, previous_block_reason=self.permission_guard_last_reason, facts=facts,
                workspace_dir=self.workspace_dir, action_id=action_id, track_id=track_id,
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
        summary, cheap and judgment-free).

        Action-boundary tracking system (2026-10-18, per the user's own
        6-point spec): runs turn.permission.identify ITSELF (not via
        _check_permission_guard) so it has matched_action_id in hand before
        deciding whether a guard call is even needed at all --
        1. identify+match against this tab's own open actions
           (self.action_registry).
        4. an action whose `approved` flag is already true is NEVER sent to
           the guard -- returns None immediately, no model call at all for
           this one. An action with no flag ever set (or not yet approved)
           falls straight through to today's unchanged guard check (point
           6), with the already-fetched facts passed through via
           _check_permission_guard's own `facts` parameter so identify
           never runs twice for the same call.
        The resolved action's id is stashed on self._action_id_for_pending_
        send regardless of outcome, so _on_action_send_completed (point 2 --
        the operation closes once actually sent) can find it once this
        call's real handler succeeds.

        Fail-open throughout, same philosophy as every other microagent
        check here: a failed summary or failed identify call just falls
        back to today's pre-existing behavior (the plain text-only guard
        check, or no check at all), never a new way to get stuck."""
        from app.plugins.voice_api import _identify_pending_action_locally, summarize_action

        try:
            summary = await summarize_action(tool_name, args)
        except Exception as exc:
            log_event("engine", "summarize_action_failed", tab_id=self.tab_id, tool=tool_name, error=str(exc))
            summary = None
        if not summary:
            return None
        log_event("engine", "action_summarized", tab_id=self.tab_id, tool=tool_name, summary=summary)

        extra_context = f"About to happen, not yet done: {summary}"
        open_actions_wire = [{"id": a.id, "summary": a.summary} for a in self.action_registry.open_actions()]
        try:
            facts = await _identify_pending_action_locally(
                self._narration_task(), self._dialogue_with_note(extra_context), open_actions=open_actions_wire,
            )
        except Exception as exc:
            log_event("engine", "action_identify_failed", tab_id=self.tab_id, error=str(exc))
            facts = None

        self._action_id_for_pending_send = None
        if facts is not None and facts.get("action_type", "none") != "none":
            matched_id = facts.get("matched_action_id")
            if matched_id and self.action_registry.get(matched_id):
                action = self.action_registry.get(matched_id)
                self.action_registry.touch(matched_id)
            else:
                from app.memory_topics import get_current_topic
                track_hint = get_current_topic(self.workspace_dir, self.tab_id)
                action = self.action_registry.open_new(facts.get("action_summary") or summary, track_hint=track_hint)
            self._action_id_for_pending_send = action.id
            self._persist_action_state()
            if action.approved:
                log_event("engine", "action_approved_short_circuit", tab_id=self.tab_id, action_id=action.id, summary=action.summary)
                return None

        await self._check_permission_guard(extra_context=extra_context, facts=facts, action_id=self._action_id_for_pending_send)
        return self.pending_unapproved_action

    def _persist_action_state(self) -> None:
        save_action_state(self.workspace_dir, self.tab_id, self.action_registry.to_wire())

    async def _check_action_sanction(self, message: str) -> None:
        """Step 3 of the action-boundary tracking system (2026-10-18): after
        every REAL user message, decide whether it grants or revokes
        sanction for any of this tab's currently open actions. Called from
        submit()'s own real-user-message path, same site _check_
        permission_guard() already runs from. Skipped entirely when
        self.action_registry is empty -- no cost when nothing is pending
        (point 6, "an action needing no separate sanction behaves exactly
        as today")."""
        if self.action_registry.is_empty():
            return
        from app.plugins.voice_api import check_action_sanction

        open_actions_wire = [{"id": a.id, "summary": a.summary} for a in self.action_registry.open_actions()]
        try:
            decisions = await check_action_sanction(message, open_actions_wire)
        except Exception as exc:
            log_event("engine", "check_action_sanction_failed", tab_id=self.tab_id, error=str(exc))
            return
        if not decisions:
            return
        changed = False
        for entry in decisions:
            decision = entry.get("decision")
            if decision == "unchanged":
                continue
            action_id = entry.get("action_id")
            if not self.action_registry.get(action_id):
                continue
            self.action_registry.set_approved(action_id, decision == "approved")
            log_event("engine", "action_sanction_changed", tab_id=self.tab_id, action_id=action_id, decision=decision)
            changed = True
        if changed:
            self._persist_action_state()

    def _on_action_send_completed(self) -> None:
        """Registered with session_context's register_action_closer; called
        by app/operations.py's dispatch() right after a send-type tool
        call's real handler succeeds -- point 2 of the action-boundary spec
        ("when the operation is done, it closes"). A no-op whenever this
        call had no action tied to it (point 6)."""
        action_id, self._action_id_for_pending_send = self._action_id_for_pending_send, None
        if not action_id:
            return
        self.action_registry.close(action_id)
        self._persist_action_state()
        log_event("engine", "action_closed", tab_id=self.tab_id, action_id=action_id)

    async def _check_reaction_score(self, message: str) -> None:
        """Agent 1 of the evolution-feedback loop (per explicit special
        order, 2026-10-19): after every real user message, scores the
        user's emotional reaction to something Caroline did on a -1
        (complete disapproval) to +1 (complete approval) scale, and tries
        to attribute it to a specific open action or this tab's current
        track (app/microagents/reaction_score.py). Called from submit()'s
        own real-user-message path, same fire-and-forget cadence as
        _check_action_sanction -- never skipped (unlike that one), since a
        reaction can be about something with no open action at all.

        Every run is logged, including the overwhelmingly common no-op case
        (no real reaction found), per explicit instruction ("Логировать
        все это надо"). A non-trivial, attributed score hands off to
        evolution.backprop (Agent 2, _run_evolution_backprop) as its own
        fire-and-forget background call -- never on this turn's own
        critical path."""
        from app.evolution_journal import recent_rounds_for
        from app.memory_topics import get_current_topic
        from app.plugins.voice_api import score_reaction

        open_actions_wire = [{"id": a.id, "summary": a.summary} for a in self.action_registry.open_actions()]
        track_id = get_current_topic(self.workspace_dir, self.tab_id)
        candidate_track_ids = [track_id] if track_id else []

        try:
            result = await score_reaction(message, open_actions_wire, candidate_track_ids)
        except Exception as exc:
            log_event("engine", "reaction_score_failed", tab_id=self.tab_id, error=str(exc))
            return
        if result is None:
            return

        score = float(result.get("reaction_score") or 0.0)
        target_action_id = result.get("target_action_id")
        target_track_id = result.get("target_track_id")
        log_event(
            "engine", "reaction_scored", tab_id=self.tab_id, reaction_score=score,
            target_action_id=target_action_id, target_track_id=target_track_id,
            dialogue_window_len=len(message),
        )
        if abs(score) < REACTION_SCORE_BACKPROP_THRESHOLD or not (target_action_id or target_track_id):
            return

        target_summary = "(unknown)"
        if target_action_id:
            action = self.action_registry.get(target_action_id)
            if action:
                target_summary = action.summary
        elif target_track_id:
            target_summary = target_track_id

        action_ids = [target_action_id] if target_action_id else None
        rounds = recent_rounds_for(self.workspace_dir, action_ids=action_ids, track_id=target_track_id, limit=5)
        asyncio.ensure_future(self._run_evolution_backprop(score, target_action_id, target_track_id, target_summary, rounds))

    async def _run_evolution_backprop(
        self, reaction_score: float, target_action_id: str | None, target_track_id: str | None,
        target_summary: str, rounds: list[dict[str, Any]],
    ) -> None:
        """Agent 2 of the evolution-feedback loop (per explicit special
        order, 2026-10-19): given Agent 1's attributed score and the recent
        tournament rounds journaled (app/evolution_journal.py) for the same
        action/track, proposes a bounded weight_selector adjustment for
        each round genuinely implicated (app/microagents/
        evolution_backprop.py) -- applied deterministically via
        app/evolution_client.py's get_role_settings/
        apply_weight_selector_delta, never by the model itself.

        Every run is logged, including the empty-adjustments case; a
        separate "backprop_applied" log line per adjustment actually
        written, so a proposed-but-skipped adjustment (a failed apply) is
        distinguishable from one that never applied in the first place --
        per explicit instruction ("Логировать все это надо"). The caller
        (_check_reaction_score) already confirmed rounds is non-empty
        before scheduling this."""
        from app.evolution_client import apply_weight_selector_delta
        from app.plugins.voice_api import run_evolution_backprop

        if not rounds:
            log_event(
                "engine", "backprop_evaluated", tab_id=self.tab_id, target_action_id=target_action_id,
                target_track_id=target_track_id, reaction_score=reaction_score, rounds_considered=[], adjustments=[],
            )
            return
        rounds_wire = [
            {"role": r["role"], "tournament_id": r["tournament_id"], "verdict_reason": r.get("verdict_reason")}
            for r in rounds
        ]
        try:
            result = await run_evolution_backprop(target_summary, reaction_score, rounds_wire)
        except Exception as exc:
            log_event("engine", "evolution_backprop_failed", tab_id=self.tab_id, error=str(exc))
            return
        adjustments = (result or {}).get("adjustments") or []
        log_event(
            "engine", "backprop_evaluated", tab_id=self.tab_id, target_action_id=target_action_id,
            target_track_id=target_track_id, reaction_score=reaction_score,
            rounds_considered=[{"role": r["role"], "tournament_id": r["tournament_id"]} for r in rounds_wire],
            adjustments=adjustments,
        )
        for adjustment in adjustments:
            role, delta = adjustment.get("role"), adjustment.get("weight_delta")
            if not role or delta is None:
                continue
            applied = await apply_weight_selector_delta(role, float(delta))
            if applied is None:
                continue
            before, after = applied
            log_event(
                "engine", "backprop_applied", tab_id=self.tab_id, role=role,
                weight_selector_before=before, weight_selector_after=after,
            )

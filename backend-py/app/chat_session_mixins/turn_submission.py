from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class TurnSubmissionMixin:
    def submit(self, text: str, attachments: list[Any] | None = None, is_real_user: bool = True, is_voice: bool = False, pending_text: str | None = None, suppress_narration: bool = False) -> None:
        # Per standing instruction ("ВЕЗДЕ логируем и ВСЁ"): this is the
        # single choke point EVERY turn goes through -- real user messages,
        # every proactive/internal nudge (inject_proactive already logs
        # its own text_len separately, but not is_real_user/is_voice), and
        # every replay -- confirmed live tonight this had NO logging of
        # its own at all, unlike inject_proactive.
        attachments = attachments or []
        # Per explicit instruction (2026-10-15): queue BEFORE anything else
        # below (the stop-note handling, memory_turns.start_turn, chat-mode
        # routing) -- see funds_blocked_queued_turns' own __init__ comment
        # for the real incident this closes off. Same early-return shape as
        # the forced-compaction queue just below, deliberately checked
        # first: if BOTH conditions somehow held at once, this still queues
        # correctly and compaction's own check simply never gets reached
        # for this call -- _drain_funds_blocked_queue() replays through this
        # exact same submit(), so compaction (if still pending by then)
        # queues it again normally, nothing is skipped or double-handled.
        funds_reason = get_funds_exhausted_reason()
        if funds_reason:
            log_event(
                "engine", "submit_queued_funds_exhausted", tab_id=self.tab_id, is_real_user=is_real_user,
                is_voice=is_voice, text_len=len(text), attachment_count=len(attachments), reason=funds_reason,
            )
            self.funds_blocked_queued_turns.append({"text": text, "attachments": attachments, "is_real_user": is_real_user, "is_voice": is_voice, "pending_text": pending_text})
            asyncio.create_task(self._publish_status())
            return
        if is_real_user and self._stop_note_pending:
            # Delivered by _push_message as its own, machine-marked block --
            # not glued into the user's words, so language detection and the
            # history readers keep seeing exactly what the user wrote.
            self._stop_note_pending = False
            self._stop_note_in_next_push = True
        if is_real_user:
            # A real message begins a turn for the memory service's money rules
            # (memory_turns.py) and supersedes a turn still waiting to be replayed.
            self._cancel_memory_replay()
            memory_turns.start_turn(self.tab_id, text, attachments, is_voice)
        if self.pending_interim_notes:
            text = self._with_interim_notes(text)
            self.pending_interim_notes = []
        if load_chat_mode(self.workspace_dir, self.tab_id) == "sw":
            self._route_sw_turn(text, attachments, is_real_user, is_voice, suppress_narration)
            return
        # Bug fix (2026-09-18), per explicit instruction after a real
        # incident: never push straight into the live client while a
        # forced compaction is still in flight for this tab -- confirmed
        # live that a real user message submitted mid-compaction raced
        # the CLI's own compact boundary and came back as a reply claiming
        # the user's own phrase "got cut off" (it genuinely had). Queue
        # here instead; _drain_compaction_queue() replays every queued
        # item, in order, through this exact same submit() once
        # forced_compaction_result_pending goes back to False -- nothing
        # about a queued item's own eventual handling is special-cased,
        # it's a completely normal submit() just delayed.
        if self.forced_compaction_result_pending:
            log_event(
                "engine", "submit_queued_during_compaction", tab_id=self.tab_id, is_real_user=is_real_user,
                is_voice=is_voice, text_len=len(text), attachment_count=len(attachments),
            )
            self.compaction_queued_turns.append({"text": text, "attachments": attachments, "is_real_user": is_real_user, "is_voice": is_voice, "pending_text": pending_text})
            asyncio.create_task(self._publish_status())
            return
        log_event(
            "engine", "submit", tab_id=self.tab_id, is_real_user=is_real_user, is_voice=is_voice,
            text_len=len(text), attachment_count=len(attachments),
        )
        if is_real_user:
            self._post_turn_check_rounds = 0
            self._post_turn_checks_halted = False
        self.classifier_refusal_retry_count = 0
        # Bug fix (2026-10-17), confirmed live -- tab 1, a real incident: this
        # used to be unconditional, same root cause as pending_is_real_user's
        # own 2026-09-15 fix just below, but for the text itself. A real user
        # question set this; while that same turn was still pending (stuck on
        # a broken OpenAI token, then a Claude rate limit), a proactive
        # silent_user_wait_nudge fired submit(is_real_user=False) and
        # overwrote this back to the nudge's own text. When the turn was then
        # hard-killed and _handle_failure replayed self.pending_user_text
        # (handle_failure_replay_pending), it replayed the NUDGE, not the
        # user's real question -- which was gone, silently, with no error and
        # no indication to the user. Same guard as pending_is_real_user: a
        # proactive/internal nudge injected INTO an already-pending turn must
        # never clobber the text of a turn a real user already started.
        if is_real_user or not self.turn_pending:
            self.pending_user_text = text
        # Reset for THIS turn -- see this flag's own __init__ comment for
        # why it exists (post-turn-completion sanity check for proactive
        # turns that silently produce no content at all).
        self.turn_saw_any_visible_text = False
        # Bug fix (2026-09-11), per explicit instruction: _gather_recent_
        # dialogue_for_narration's own pending_user_text fallback used to
        # label THIS unconditionally as if the user had just said it --
        # confirmed live: an internal retry nudge ("[Internal: automatic
        # recheck... Reply in English.") got fed to the narrator as literal
        # user speech, producing both nonsense commentary and an English
        # reply for an otherwise-Russian conversation. Tracked alongside
        # pending_user_text so that fallback can tell the difference.
        #
        # Bug fix (2026-09-15), confirmed live -- tab 4, right after this
        # same session's app restart: a real user message set this True,
        # then (mid-turn, while turn_pending was ALREADY True from that
        # same real question) a background-operation-completion nudge
        # fired via inject_proactive -> submit(is_real_user=False), which
        # unconditionally overwrote this back to False -- silencing the
        # narrator for the rest of that turn even though the model was
        # still working on nothing but the user's own real request the
        # whole time. A proactive/internal nudge injected INTO an
        # already-pending turn is additive content for that same turn,
        # not the start of a new one -- it must never downgrade an
        # already-True flag. Only actually reset the flag (to whatever
        # is_real_user says) when this submit() is starting a genuinely
        # NEW turn (self.turn_pending wasn't already True) -- a real
        # user message still always wins immediately either way.
        if is_real_user or not self.turn_pending:
            self.pending_is_real_user = is_real_user
            self._turn_last_visible_text = None
        # Same "only a genuinely new turn" guard as pending_is_real_user
        # just above -- see current_turn_suppress_narration's own __init__
        # comment for why this must never retroactively silence narration
        # on a turn that was already in flight.
        #
        # Bug fix (2026-10-19), confirmed live: this used to be gated on
        # `not self.turn_pending` alone, unlike every sibling flag in this
        # function (pending_user_text/pending_is_real_user/pending_
        # attachments), which all give is_real_user the same override. A
        # suppress_narration=True proactive nudge (e.g. _check_forced_
        # compaction's memory-flush-before-compaction inject) that landed
        # while turn_pending never cleanly dropped back to False left this
        # flag stuck True for the rest of the tab's life -- a real user's
        # own brand new message had no way to reclaim narration for itself,
        # unlike it could for the other flags. Confirmed live: a tab's
        # narrator went completely silent (not even a skip/retry log line)
        # for 41+ hours after exactly one such flush, across many real
        # user turns in between. is_real_user now wins here too, same as
        # its neighbors -- the protection the comment above describes (a
        # proactive nudge must never retroactively silence an ALREADY
        # narrating turn) is unaffected, since that case never has
        # is_real_user=True in the first place.
        if is_real_user or not self.turn_pending:
            self.current_turn_suppress_narration = suppress_narration
        # Same guard as pending_user_text just above, same incident: a real
        # user message's attachments must not be wiped out by a later
        # proactive nudge (normally attachment-less) arriving mid-turn --
        # _handle_failure's replay needs the real ones still there.
        if is_real_user or not self.turn_pending:
            self.pending_attachments = attachments
        self.turn_pending = True
        self.last_activity = time.monotonic()
        if is_real_user:
            self.last_user_activity = time.monotonic()
            self.last_real_user_turn_at = time.monotonic()
            # Bug fix (2026-09-16), per explicit instruction: "нарратор
            # должен комментировать только текущую задачу" -- last_real_
            # user_turn_at above is monotonic (process-uptime-relative),
            # not comparable to the transcript's own wall-clock
            # timestamps. This is the SAME moment, in wall-clock epoch-ms,
            # so _gather_recent_dialogue_for_narration can use it as a
            # floor -- excluding anything left over from a previous,
            # already-finished task instead of a blind last-N-lines window
            # that doesn't know where the current task actually started.
            self.last_real_user_turn_started_at_ms = time.time() * 1000
            self.real_user_turn_answered = False
            self.silence_nudge_sent_for_turn = False
            # A genuine new real-user episode starts with a clean ground-truth
            # slate -- see the field's own __init__ comment for why this is
            # episode-scoped rather than per-turn. A leftover in-flight tool
            # call from whatever the PREVIOUS episode was doing can never be
            # resolved now (that episode is over), so it's discarded here
            # too rather than left to dangle in _pending_tool_calls forever.
            self._pending_tool_calls = {}
            self.turn_tool_outcomes = {}
            self.turn_narration_remarks = []
            self.last_visible_output_at = time.monotonic()
            # Bug fix (2026-09-14): a genuine new real message is the one
            # thing that should give this a clean slate -- see its own
            # __init__ comment for the incident this fixes.
            self.consecutive_narration_count = 0
            self.last_real_user_question = text
            # Per explicit instruction (2026-10-13): the approval/prohibition bridge's
            # own raw material -- see self.recent_real_user_messages' own __init__
            # comment. Every real user message, pruned to the last hour on each append
            # so this never grows unbounded across a long-lived tab.
            now_ms = time.time() * 1000
            self.recent_real_user_messages.append((now_ms, text))
            self.recent_real_user_messages = [
                (t, m) for t, m in self.recent_real_user_messages if now_ms - t <= 3600_000
            ]
            # Bug fix (2026-10-15): used to unconditionally zero permission_guard_
            # consecutive_blocks here -- removed. A genuine new real-user message is
            # NOT, on its own, evidence the pending action changed (it's very often a
            # CLARIFICATION of the exact same one) -- see permission_guard_last_reason's
            # own __init__ comment: the counter now only resets in _check_permission_
            # guard, when turn.permission.guard itself judges the action has actually
            # changed (same_action_as_before=False), not merely because the user spoke.
            # Per explicit instruction (2026-10-11): a just-given "yes" must clear
            # pending_unapproved_action before the model's NEXT turn starts, not
            # only the next time an outbound message triggers a re-check -- fire-
            # and-forget, same mechanism as refresh_language_in_background below.
            asyncio.ensure_future(self._check_permission_guard())
            # Per explicit instruction (2026-10-18), the action-boundary
            # tracking system's point 3: every real user message is also
            # checked for whether it grants/revokes sanction on any of this
            # tab's currently open actions -- same fire-and-forget shape as
            # the permission-guard recheck just above, and a no-op (see
            # _check_action_sanction's own early return) whenever nothing is
            # open.
            asyncio.ensure_future(self._check_action_sanction(text))
            # Per explicit special order (2026-10-19; reworked 2026-10-24): every
            # real user message is also scored for an emotional reaction, and the
            # score goes to the evolution as the reaction to the previous turn --
            # same fire-and-forget shape as the two checks just above. The
            # previous turn's id is taken here, while it is still the one this
            # message's turn replaced. Kept: the start-rung advice of this message reads the
            # score when it is ready in time (chat_session_mixins/ladder.py, app/rung_advice.py).
            self._reaction_task = asyncio.ensure_future(self._check_reaction_score(text, memory_turns.previous_turn_id(self.tab_id)))
            # Bug fix (2026-09-11), per explicit instruction: language must
            # be tracked CONTINUOUSLY, not resolved once and left alone --
            # confirmed live a tab can legitimately switch languages
            # mid-conversation (e.g. the user asking for a contract drafted
            # in three languages), and a one-shot resolution (at startup,
            # or only as a side effect of an unrecoverable-session-reset)
            # can never catch that. Every real user turn re-triggers it --
            # fire-and-forget, same mechanism as before, just called far
            # more often (as often as the user actually talks) instead of
            # a handful of one-off lifecycle events. Reads whatever's on
            # disk as of THIS call (not including the text being submitted
            # right now, which hasn't been flushed to the transcript yet)
            # -- a real language switch shows up starting from the
            # FOLLOWING turn, not instantly; an acceptable lag, not a
            # correctness gap (current_language_name() is only ever read
            # at the next query()/narration tick anyway).
            refresh_language_in_background(self.last_saved_session_id, self.tab_id)
            # Per explicit instruction (2026-09-14): this tab's 24h-dialogue
            # file is refreshed on every real user turn -- see
            # _write_recent_24h_dialogue_file's own docstring. Bug fix
            # (2026-09-20): no longer synchronous on the event loop (see
            # _schedule_recent_24h_dialogue_refresh) -- the path is stable,
            # so setting it now is instant; the content catches up in a
            # worker thread within a moment, long before the model reads it.
            self._recent_24h_dialogue_file_path = str(_recent_24h_dialogue_path(self.workspace_dir, self.tab_id))
            self._schedule_recent_24h_dialogue_refresh()
        # pending_text: what a restart should resume FROM -- the user's own words, not this
        # call's (possibly wrapped) wire text. See durability.unwrap_resume_note for the
        # nested-wrapper incident this prevents.
        #
        # Issue #3 (2026-10-24): a nudge nobody typed (the startup greeting, a
        # reminder, a background note) never replaces a pending turn already on
        # disk -- that record may be the user's only copy of unfinished work, and a
        # crash would then resume the nudge instead of it.
        if is_real_user or peek_pending_turn(self.workspace_dir, self.tab_id) is None:
            save_pending_turn(self.workspace_dir, self.tab_id, pending_text if pending_text is not None else text, attachments)
        else:
            log_event("engine", "pending_turn_kept_for_real_user_work", tab_id=self.tab_id, is_real_user=is_real_user)
        # Bug fix (2026-09-10): tag the WIRE copy (never pending_user_text/
        # last_real_user_question/the saved pending-turn file above -- those
        # all need to stay the real, clean text for their own consumers,
        # e.g. main.py's crash-resume path or a restart replay) so a reader
        # of the saved transcript can tell a proactive/synthetic turn from
        # a real one structurally, without guessing from its wording -- see
        # _SYNTHETIC_TURN_MARKER's own docstring.
        wire_text = text if is_real_user else f"{_SYNTHETIC_TURN_MARKER}{text}"
        self._push_message(wire_text, attachments, is_voice, memory_advice_for=text if is_real_user else None)
    def submit_or_try_small_model(self, text: str, attachments: list[Any] | None = None, is_voice: bool = False, is_real_user: bool = True) -> None:
        """The entry point for what the USER sends (the chat window, the
        REST /api/message, the phone). In "sw" mode submit() itself routes
        every turn to the small-model path.

        Per explicit instruction (2026-10-10): consecutive user messages go
        to the model as ONE. Seen live: four short messages typed one after
        another reached the Claude CLI as four separate user messages, each
        cutting into the work the previous one had started. A user message
        is held for USER_MESSAGE_MERGE_WINDOW_S; each further one within
        that time joins it and restarts the wait; then they are submitted
        together. Internal turns and replays call submit() directly and are
        never held.

        Continuous dialogue (see app/continuous_dialogue.py): while that setting is on, what the user
        sends is a voice turn whatever way it was sent."""
        if not is_real_user:
            self.submit(text, attachments, is_real_user, is_voice)
            return
        from app.continuous_dialogue import is_enabled as continuous_dialogue_enabled

        is_voice = is_voice or continuous_dialogue_enabled(self.workspace_dir)
        self._held_user_messages.append((text, list(attachments or []), is_voice))
        if not self.headless:
            from app import dialogue_log

            dialogue_log.append(self.workspace_dir, self.tab_id, dialogue_log.USER,
                                text + (f"\n[{len(attachments)} attachment(s)]" if attachments else ""), voice=is_voice)
        # Bug fix (2026-10-26), found by closing the window while a message was still held: submit()
        # is what saves the pending turn, and it only runs when the merge window ends -- a session
        # disposed before that took the user's request with it, and nothing was resumed. Saved now,
        # for the held messages as one (what submit() will be handed), and overwritten there as usual.
        save_pending_turn(
            self.workspace_dir, self.tab_id, "\n\n".join(t for t, _a, _v in self._held_user_messages if t),
            [att for _t, atts, _v in self._held_user_messages for att in atts],
        )
        if self._held_user_flush is not None:
            self._held_user_flush.cancel()
        self._held_user_flush = asyncio.get_running_loop().call_later(USER_MESSAGE_MERGE_WINDOW_S, self._flush_held_user_messages)
        log_event("engine", "user_message_held", tab_id=self.tab_id, held=len(self._held_user_messages), text_len=len(text))
        self._start_fast_lane_ahead()
    def _with_interim_notes(self, text: str) -> str:
        """`text` as submit() hands it on: led by the narration remarks of the previous
        request, when there were any."""
        if not self.pending_interim_notes:
            return text
        notes = "\n".join(f"- {note}" for note in self.pending_interim_notes)
        return (
            f"{_INTERIM_NOTES_HEADER}while working on the previous request, "
            "marked as progress updates -- your own past words, do not repeat them as news:\n"
            f"{notes}]\n\n{text}"
        )
    def _start_fast_lane_ahead(self) -> None:
        """See FAST_LANE_DURING_MERGE_WINDOW. Asks the fast lane exactly what
        _run_small_model_turn will ask if nothing changes before the window closes;
        _fast_lane_for_turn uses the result only if that is still the question."""
        if self._fast_lane_ahead is not None:
            self._fast_lane_ahead[1].cancel()
            self._fast_lane_ahead = None
        if not FAST_LANE_DURING_MERGE_WINDOW or self.ended or self.small_model_active:
            return
        held = self._held_user_messages
        if any(attachments for _text, attachments, _voice in held) or not any(voice for _text, _attachments, voice in held):
            return
        if load_chat_mode(self.workspace_dir, self.tab_id) != "sw":
            return
        text = self._with_interim_notes("\n\n".join(item_text for item_text, _attachments, _voice in held if item_text))
        lines = _read_recent_dialogue_lines(self.last_saved_session_id, self.tab_id, self.workspace_dir, RECENT_DIALOGUE_WINDOW)
        language = current_language_name(self.tab_id)
        log_event("engine", "fast_lane_ahead_started", tab_id=self.tab_id, text_len=len(text), held=len(held))
        task = asyncio.create_task(self._fast_lane_ahead_body(get_persona(self.workspace_dir), text, lines, language))
        self._fast_lane_ahead = ((text, language, tuple(lines)), task)
    async def _fast_lane_ahead_body(
        self, persona: Any, text: str, lines: list[str], language: str,
    ) -> tuple[str | None, int | None, str | None, asyncio.Task[str | None] | None]:
        fast, start_tier, first_remark = await try_fast_answer(self.workspace_dir, persona, text, lines, language, True)
        # The answer's review as well, inside the window: it is the text that will be
        # reviewed when the turn starts (the review comes before any translation).
        if fast:
            self._review_ahead(fast, text)
            self._speech_ahead(fast, language)
        # The remark's review and audio too, while the window is still running -- in a
        # task of its own: a turn that starts meanwhile must not wait for it before it
        # gets to work. The task returns (the remark as reviewed, its audio).
        audio = asyncio.create_task(self._review_and_synthesize(first_remark, text)) if first_remark else None
        return fast, start_tier, first_remark, audio
    def _review_ahead(self, text: str, task: str) -> None:
        """Starts reply.review for a text that will most likely be the final answer --
        the fast lane's answer during the merge window, a tier's answer while the
        verifier is still checking it (it says "OK" to most) -- so the answer does not
        wait for the review after everything else is done. Measured 2026-10-12, one
        simple question: tier 4s, verifier 3.8s, then the review 5s on top, 18s in all.
        _reviewed takes the result if that exact text is what goes out; a call for a
        text that did not go out is dropped."""
        key = (text, task)
        if key in self._reviews_ahead:
            return

        async def run() -> str | None:
            from app.plugins.voice_api import review_reply

            try:
                return await review_reply(text, task=task)
            except Exception as exc:  # noqa: BLE001 -- fail-open, as review_reply itself is
                log_event("engine", "review_reply_failed", tab_id=self.tab_id, error=str(exc))
                return None

        self._reviews_ahead[key] = asyncio.create_task(run())
    def _speech_ahead(self, text: str, language: str) -> None:
        """The first pieces of a spoken reply synthesized while it is still being checked
        -- see speech_ahead.py. Only for a text that will go out as it is: one that still
        has to be translated is not the text chat.js will read."""
        if not answer_is_in_language(text, language):
            return
        from app import speech_ahead
        from app.plugins.voice_api import language_code_for_name

        try:
            speech_ahead.prepare(text, get_persona_gender(self.workspace_dir), self.tab_id, language_code_for_name(language))
        except Exception as exc:  # noqa: BLE001 -- a head start must never cost the turn
            log_event("engine", "speech_ahead_failed", tab_id=self.tab_id, error=str(exc)[:200])
    async def _reviewed(self, text: str, task: str) -> str | None:
        """reply.review's result for a final text: the call already under way for exactly
        this text and task if there is one, a new call otherwise. Everything else started
        ahead is for a text that is not going out, and is dropped."""
        from app.plugins.voice_api import review_reply

        ahead, self._reviews_ahead = self._reviews_ahead, {}
        mine = ahead.pop((text, task), None)
        for other in ahead.values():
            other.cancel()
        if mine is not None:
            log_event("engine", "review_reply_ahead_used", tab_id=self.tab_id, ready=mine.done())
            return await mine
        return await review_reply(text, task=task)
    async def _fast_lane_for_turn(
        self, persona: Any, text: str, lines: list[str], language: str, is_voice: bool,
    ) -> tuple[str | None, int | None, str | None, asyncio.Task[str | None] | None]:
        """try_fast_answer for the turn starting now -- taken from the call made ahead
        during the merge window when that one asked exactly this question of exactly
        this dialogue; otherwise asked now."""
        ahead, self._fast_lane_ahead = self._fast_lane_ahead, None
        if ahead is not None:
            key, task = ahead
            if is_voice and key == (text, language, tuple(lines)):
                log_event("engine", "fast_lane_ahead_used", tab_id=self.tab_id, ready=task.done())
                return await task
            log_event("engine", "fast_lane_ahead_discarded", tab_id=self.tab_id)
            task.cancel()
        fast, start_tier, first_remark = await try_fast_answer(self.workspace_dir, persona, text, lines, language, is_voice)
        return fast, start_tier, first_remark, None
    def _flush_held_user_messages(self) -> None:
        held, self._held_user_messages = self._held_user_messages, []
        self._held_user_flush = None
        if not held or self.ended:
            return
        text = "\n\n".join(item_text for item_text, _attachments, _voice in held if item_text)
        attachments = [attachment for _text, item_attachments, _voice in held for attachment in item_attachments]
        is_voice = any(voice for _text, _attachments, voice in held)
        if len(held) > 1:
            log_event("engine", "user_messages_merged", tab_id=self.tab_id, count=len(held), text_len=len(text))
        self.submit(text, attachments, True, is_voice)
    def _route_sw_turn(self, text: str, attachments: list[Any], is_real_user: bool, is_voice: bool, suppress_narration: bool = False) -> None:
        """Every turn in "sw" mode goes here, never to the Claude SDK (2026-10-03):
        while a small-model turn runs, new input is live context for it -- UNLESS it's a
        real user message arriving during a PROACTIVE turn, which queues as its own fresh
        turn instead (see small_model_queued_real_turns). Otherwise a new turn starts."""
        if self.small_model_active:
            if is_real_user and not self.small_model_turn_is_real_user:
                log_event("engine", "small_model_real_turn_queued_behind_proactive", tab_id=self.tab_id, text_len=len(text))
                self.small_model_queued_real_turns.append((text, attachments, is_voice))
                return
            if is_real_user:
                self.turn_is_voice = self.turn_is_voice or is_voice
            log_event("engine", "small_model_live_comment_queued", tab_id=self.tab_id, text_len=len(text), is_real_user=is_real_user)
            self.small_model_pending_comments.append(text if is_real_user else f"{_SYNTHETIC_TURN_MARKER}{text}")
            return
        self._start_small_model_turn(text, attachments, is_real_user, is_voice, suppress_narration)
    def inject_proactive(self, text: str, attachments: list[Any] | None = None, is_voice: bool = False, pending_text: str | None = None, suppress_narration: bool = True) -> bool:
        """Bug fix (2026-09-11), per explicit instruction: no more
        silent/silent_turn parameter -- whether a proactive reply is worth
        showing is now decided ENTIRELY by the model's own reply content
        (the existing [[NO_UPDATE]] sentinel, see no_update_sentinel_instruction),
        never by a caller-side flag here. silent_turn used to be a
        whole-session AND-latch that (a) couldn't be un-set once a real
        conversation had made it False (confirmed live: an hourly
        "silent" reminder could still leak into the chat this way) and
        (b) got its own state corrupted by an unrelated bug (a rejected
        turn's own trailing ResultMessage wrongly flipping it back to
        True mid-conversation, silently swallowing a whole later real
        reply). Removed entirely rather than patched again.

        `suppress_narration` default flipped to True (2026-10-18), per
        explicit instruction ("Нарратора у фоновых активностей быть не
        должно вообще" -- confirmed live, screenshots): every inject_
        proactive() caller is, by construction, a background/proactive
        event (a messenger notification, a reminder, a watchdog nudge --
        never a real user message, which always goes through submit()
        directly with is_real_user=True instead). The progress narrator's
        own job is to keep a REAL user company while Caroline is visibly
        mid-task on something THEY asked for; confirmed live it has no
        reliable way to tell a genuine in-flight user task apart from
        stale/unrelated internal chatter once a proactive note is in the
        dialogue (it kept narrating a 'still backing up your notes/updating
        storage' status for 40+ minutes with no real backup operation
        actually running). Previously only reminder-driven turns
        (main.py's _on_reminder_due) and a handful of other call sites
        opted into this explicitly; messenger-channel notifications
        (_inject_messenger_proactive) and others relied on the old
        default (False) and got narration they were never supposed to.
        A caller that genuinely wants narration during a proactive turn
        must now opt in explicitly with suppress_narration=False -- none
        currently do, and none are expected to."""
        if self.ended:
            return False
        attachments = attachments or []
        log_event("engine", "proactive_inject", tab_id=self.tab_id, text_len=len(text), attachment_count=len(attachments))
        asyncio.create_task(self.send({"type": "proactive_turn_queued"}))
        # Per explicit instruction (2026-10-11): a proactive note is often exactly
        # the thing that says whose account/identity a reply would go out under
        # (e.g. "your own Telegram account" vs "the owner's own correspondence,
        # not yours") -- text the permission guard would otherwise never see, since
        # _gather_recent_dialogue_for_narration drops synthetic notes on purpose
        # (right for the narrator, wrong for this). Fired before submit() queues
        # the note for the model to act on, so it has a head start on the model's
        # own (typically slower) turn -- best-effort, same fail-open philosophy as
        # every other call to this, not a hard guarantee of ordering.
        asyncio.ensure_future(self._check_permission_guard(extra_context=text))
        # Internal turns follow the same chat-mode gate as real ones (2026-10-03):
        # in "sw" mode they run through the small-model cascade; everything else
        # (other modes, attachments, pending_text replays) keeps the SDK path.
        self.submit(text, attachments, False, is_voice, pending_text=pending_text, suppress_narration=suppress_narration)
        return True

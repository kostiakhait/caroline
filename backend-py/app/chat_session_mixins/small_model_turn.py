from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class SmallModelTurnMixin:
    def _start_small_model_turn(self, text: str, attachments: list[Any], is_real_user: bool, is_voice: bool) -> None:
        self.small_model_turn_is_real_user = is_real_user
        # Bug fix (2026-10-09): turn_is_voice was only ever set by _input_stream, which an
        # sw-mode turn never goes through -- so every narration remark of an sw turn went
        # out with isVoice=False and was never spoken or animated, voice turn or not.
        self.turn_is_voice = is_voice
        log_event("engine", "small_model_turn_attempting", tab_id=self.tab_id, text_len=len(text), is_real_user=is_real_user)
        model_text = text
        if attachments:
            # Saved to workspace/uploads/ like the SDK path does, so tools can open the file by path.
            saved_lines = []
            for a in attachments:
                if isinstance(a, dict) and a.get("dataBase64"):
                    saved_path = _save_attachment_to_uploads(a)
                    saved_lines.append(f"- {saved_path} ({a.get('mimeType') or 'unknown type'})")
                else:
                    saved_lines.append(f"- {a.get('name') if isinstance(a, dict) else a} (not saved: no data)")
            model_text = f"{text}\n[The user attached these files, saved locally:\n" + "\n".join(saved_lines) + "\n]"
        # Mirrors submit()'s own is_real_user bookkeeping (see its comments for
        # why each field exists). Real-user-only state (silent-wait nudge, crash
        # recovery of a pending question) is skipped for internal turns.
        self.classifier_refusal_retry_count = 0
        self.pending_user_text = text
        self.pending_is_real_user = is_real_user
        self._turn_last_visible_text = None
        self.pending_attachments = []
        self.turn_pending = True
        self.last_activity = time.monotonic()
        self.last_user_activity = time.monotonic()
        self.last_visible_output_at = time.monotonic()
        self.consecutive_narration_count = 0
        self._pending_tool_calls = {}
        self.turn_tool_outcomes = {}
        if is_real_user:
            self._post_turn_check_rounds = 0
            self._post_turn_checks_halted = False
            self.last_real_user_turn_at = time.monotonic()
            self.last_real_user_turn_started_at_ms = time.time() * 1000
            self.real_user_turn_answered = False
            self.silence_nudge_sent_for_turn = False
            self.last_real_user_question = text
            # Per explicit instruction (2026-10-11): same re-check as the SDK
            # path's own submit() -- see that call site's comment for why.
            asyncio.ensure_future(self._check_permission_guard())
            refresh_language_in_background(self.last_saved_session_id, self.tab_id)
            save_pending_turn(self.workspace_dir, self.tab_id, text, [])

        self.small_model_active = True
        self.small_model_live_dialogue = None
        self.small_model_activity = []
        self._reset_voice_narration()
        self._narration_turn_started_at = time.monotonic()
        self._small_model_task = asyncio.create_task(self._run_small_model_turn(model_text, is_voice, is_real_user))
    def _end_small_model_turn(self, start_queued_real_turn: bool = True) -> None:
        self.small_model_active = False
        self.turn_is_voice = False
        self._reset_voice_narration()
        self.small_model_live_dialogue = None
        self.small_model_pending_comments = []
        self._small_model_task = None
        if start_queued_real_turn and self.small_model_queued_real_turns:
            text, attachments, is_voice = self.small_model_queued_real_turns.pop(0)
            log_event("engine", "small_model_queued_real_turn_starting", tab_id=self.tab_id, text_len=len(text), remaining=len(self.small_model_queued_real_turns))
            self._start_small_model_turn(text, attachments, True, is_voice)
    async def _run_small_model_turn(self, text: str, is_voice: bool, is_real_user: bool = True) -> None:
        # Bug fix (2026-10-15), confirmed live ("вкладка 2, где нарратор?"):
        # same root cause as the "sw chat mode" fix just above this method
        # (search its own comment for "LOOKED dead while it wasn't") --
        # conn_state is set to "limited"/"restarting"/etc. ONLY by the SDK
        # engine's own message loop, and NOTHING ever reset it back to
        # "connected" when a turn instead gets answered via this small-model
        # fallback path (e.g. Codex hit its usage limit mid-turn, self-heal
        # escalated to small_model_engine.py to actually finish the work).
        # _check_progress_narration's own gate (`conn_state.get("kind") !=
        # "connected"`) then stayed permanently closed for the rest of the
        # session, even while this method's own real tool activity kept
        # right on going underneath it -- the turn was never actually
        # finished, but the narrator had no way to know that, because the
        # one signal it trusts for "is anything really happening" was stuck
        # reporting the OLD engine's failure. Reset here, not only at
        # _finish_small_model_turn_answered's very end, so narration (and
        # anything else gated on conn_state) is unblocked for the actual
        # DURATION of this fallback's work, not just after it's already over.
        self._set_conn_state("connected")
        persona = get_persona(self.workspace_dir)
        recent_dialogue_lines = _read_recent_dialogue_lines(
            self.last_saved_session_id, self.tab_id, self.workspace_dir, RECENT_DIALOGUE_WINDOW,
        )
        language = current_language_name(self.tab_id)
        history_messages, history_summary = await self._sw_history_context()

        def on_live_dialogue_update(lines: list[str]) -> None:
            self.small_model_live_dialogue = lines

        def get_new_user_comments() -> list[str] | None:
            if not self.small_model_pending_comments:
                return None
            comments = self.small_model_pending_comments
            self.small_model_pending_comments = []
            return comments

        # This turn's own list (_start_small_model_turn just made it). Seen live
        # (2026-10-10): after Stop, the cancelled turn's worker kept calling tools for
        # another ~30s, and its failures landed in the NEXT turn's facts -- the narrator
        # then told the user about a search the new request never made.
        turn_activity = self.small_model_activity

        def on_activity(line: str) -> None:
            if self.small_model_activity is not turn_activity:
                return
            # Ported from a teammate's branch (23665f1): a multi-line tool
            # result (e.g. web_search) could otherwise smuggle a fact past
            # line-based processing elsewhere -- one fact is one line.
            self.small_model_activity.append(" ".join(line.split())[:300])
            del self.small_model_activity[:-20]
            if self._lines_since_answer_candidate is not None:
                self._lines_since_answer_candidate += 1
            # A new fact is what the narrator waits for: drafting starts now, not at the
            # loop's next tick (up to a second later).
            self._voice_narration_wake.set()
            # Bug fix (2026-10-03), explicit instruction: status is push-on-CHANGE
            # (turn_pending flips true once, _publish_status fires once, then nothing
            # republishes it) -- fine for the SDK path, where real SDK stream messages
            # keep last_activity/the UI otherwise occupied, but confirmed live for an
            # sw-mode turn running many minutes (an 8-mailbox check escalating through
            # tiers): the client's own "Working" display went stale well before the
            # turn actually finished, with no backend signal telling it anything was
            # still happening. Republish on every real operation -- same content each
            # time ("working"), but keeps a long-running turn's status visibly fresh
            # until the turn itself actually ends, not just until the first tick.
            asyncio.create_task(self._publish_status())

        translate_answer = True
        try:
            fast = None
            start_tier = None
            # Fast lane (2026-10-10 prototype, see small_model_engine.try_fast_answer):
            # real user questions only, and not with attachments (those need tools).
            if is_real_user and "[The user attached these files" not in text:
                fast, start_tier, first_remark, first_audio = await self._fast_lane_for_turn(
                    persona, text, recent_dialogue_lines, language, is_voice,
                )
                if fast is None and first_remark and self._voice_narration_applies():
                    self._first_voice_remark = first_remark
                    self._first_voice_remark_covers_start = True
                    self._first_voice_remark_task = asyncio.create_task(
                        self._say_first_voice_remark(self._narration_turn_seq, first_remark, first_audio)
                    )
            if fast is not None:
                result = {"status": "answered", "text": fast}
                translate_answer = not answer_is_in_language(fast, language)
            else:
                result = await run_small_model_turn(
                    tab_id=self.tab_id, workspace_dir=self.workspace_dir, persona=persona, user_text=text,
                    recent_dialogue_lines=recent_dialogue_lines, language=language,
                    send=self.send, on_live_dialogue_update=on_live_dialogue_update,
                    get_new_user_comments=get_new_user_comments, on_activity=on_activity,
                    start_tier=start_tier, is_voice=is_voice,
                    history_messages=history_messages, history_summary=history_summary,
                    recorder=self._sw_turn_recorder,
                    # Review of a tier's answer runs alongside its verification (_review_ahead);
                    # the voice narrator holds back meanwhile (_answer_being_checked).
                    on_candidate=(
                        (lambda candidate: (self._note_answer_candidate(), self._review_ahead(candidate, text),
                                            is_voice and self._speech_ahead(candidate, language)))
                        if is_real_user else None
                    ),
                )
                if CASCADE_SKIP_TRANSLATION_WHEN_IN_LANGUAGE:
                    translate_answer = not answer_is_in_language(result["text"], language)
        except asyncio.CancelledError:
            # Stop button -- see stop()'s own small_model_active branch. Never starts a
            # queued real turn -- an explicit Stop means stop, not run the next thing.
            log_event("engine", "small_model_turn_cancelled", tab_id=self.tab_id)
            clear_pending_turn(self.workspace_dir, self.tab_id)
            self.turn_pending = False
            self._end_small_model_turn(start_queued_real_turn=False)
            raise
        except Exception as exc:  # noqa: BLE001 -- this path must never wedge the session
            log_event("engine", "small_model_turn_unexpected_error", tab_id=self.tab_id, error=str(exc), error_type=type(exc).__name__)
            result = {"status": "answered", "text": "I can't answer this right now -- please try again a little later."}

        question_text = text if is_real_user else f"{_SYNTHETIC_TURN_MARKER}{text}"
        await self._finish_small_model_turn_answered(question_text, result["text"], is_voice, translate=translate_answer)
    async def _sw_history_context(self) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Phase 2b (2026-10-22, see app/sw_history.py): makes sure this turn's
        session exists, waits for the previous turn's background work (its
        tool-work summary, a compaction) so the history is complete, starts a
        recorder for this turn's tool calls, and returns the conversation so
        far as (messages, compressed-summary section). (None, None) when the
        sw history is still empty -- e.g. a tab whose earlier turns ran on
        Claude -- and the turn falls back to recent_dialogue_lines."""
        self._sw_turn_recorder = None
        session_id = _ensure_small_model_session(self.workspace_dir, self.tab_id, self.last_saved_session_id)
        if session_id is None:
            return None, None
        self.last_saved_session_id = session_id
        job = self._sw_history_job
        if job is not None and not job.done():
            try:
                await asyncio.wait_for(asyncio.shield(job), timeout=SW_HISTORY_WAIT_S)
            except asyncio.TimeoutError:
                log_event("engine", "sw_history_wait_timed_out", tab_id=self.tab_id, waited_s=SW_HISTORY_WAIT_S)
        self._sw_turn_recorder = sw_history.TurnRecorder(self.workspace_dir, session_id)
        summary, turns = sw_history.load(self.workspace_dir, session_id)
        if not turns and not summary:
            return None, None
        return sw_history.context_messages(turns), sw_history.summary_section(summary)
    async def _finish_small_model_turn_answered(self, question_text: str, text: str, is_voice: bool, translate: bool = True) -> None:
        """Synthesizes the same {assistant} + {result} wire pair a normal
        SDK turn's completion sends (see wire.py's message_to_wire), so
        chat.js needs no changes at all to render a small-model answer --
        it can't tell the difference from a real SDK turn's own reply.

        Also persists this exchange onto the tab's own SDK session .jsonl
        (see _append_small_model_turn_to_session's own docstring) -- both
        engines answer the same conversation and get used interchangeably
        turn by turn, so both must leave the SAME durable history behind,
        not two that silently diverge.

        Per explicit instruction (2026-09-15): forced-translates `text`
        here too, same as the full SDK path's own _translate_wire_visible_
        text -- this path is just as much a "real visible reply" as that
        one, and the small model's own language compliance is no more
        reliable than the full model's. Persists the TRANSLATED text (not
        the original) to the session .jsonl -- that's genuinely what
        Caroline said to the user, and every later reader of that history
        (recent-dialogue narration input, the 24h-dialogue file, a resumed
        SDK session) should see the same thing the user actually saw."""
        from app.plugins.voice_api import review_reply, translate_text

        # Same rule as the SDK path (_strip_no_update_from_wire): a silent
        # [[NO_UPDATE]] reply never reaches the visible dialog.
        silent = _NO_UPDATE_SENTINEL in text or _is_silent_reply_paraphrase(text)
        # Per explicit instruction (2026-10-13): review BEFORE translate, not
        # after -- reply.review needs the real original content to judge task
        # relevance/internal-mechanics leaks; reviewing already-translated text
        # was confirmed live to produce broken, often English, output
        # disconnected from what was actually said (review's own LLM defaulting
        # to English when asked to rewrite non-English input with no
        # instruction to preserve its language). Translating AFTER review
        # instead means whatever review actually decides to show is what gets
        # put into the right language, every time.
        # The final text's speech, alongside its review (2026-10-15): when the verifier
        # corrected the answer, nothing had been made ahead for the corrected text, and
        # live its first piece took 11.8s to synthesize after the review. The review
        # leaves a text unchanged far more often than not.
        if is_voice and not silent:
            self._speech_ahead(text, current_language_name(self.tab_id))
        try:
            reviewed = await self._reviewed(text, question_text)
        except Exception as exc:
            log_event("engine", "review_reply_failed", tab_id=self.tab_id, error=str(exc))
            reviewed = None
        if reviewed and reviewed != text:
            text = reviewed
        # review_reply can itself decide a message is internal-mechanics-only
        # and rewrite it down to the [[NO_UPDATE]] sentinel (or a silent-reply
        # paraphrase) -- confirmed live (2026-10-13): this was never checked
        # for afterward, so a would-be-silent rewrite just got translated and
        # shown anyway. Same rule as the original check above.
        if not silent and (_NO_UPDATE_SENTINEL in text or _is_silent_reply_paraphrase(text)):
            silent = True
            log_event("engine", "review_reply_produced_no_update", tab_id=self.tab_id, text=text)
        # translate=False: an answer already checked to be in the tab's language
        # (answer_is_in_language) -- the pass took 4-20s live and changed nothing.
        # No translation either while this tab's language is still unknown (its
        # first turn): current_language_name() then falls back to "English", and
        # the pass translated a correct Russian "Понял" into "I understood."
        # (seen on the test box, 2026-10-22). The model already answers in the
        # user's language; the language is resolved from this turn right after
        # it is persisted, so later turns are checked as before.
        if not silent and translate and _load_persisted_language(self.tab_id) is not None:
            try:
                translated = await translate_text(
                    text, current_language_name(self.tab_id), gender=get_persona_gender(self.workspace_dir),
                    addressee_gender=get_owner_profile(self.workspace_dir).gender, context=question_text,
                )
            except Exception as exc:
                log_event("engine", "small_model_translate_failed", tab_id=self.tab_id, error=str(exc))
                translated = None
            if translated and translated != text:
                log_event("engine", "small_model_translate_applied", tab_id=self.tab_id, original_len=len(text), translated_len=len(translated))
                text = translated
        # Per explicit instruction (2026-10-11): re-evaluate the permission
        # guard on every outbound message -- see _check_permission_guard's
        # own docstring for why this runs here too, not just in submit().
        # Not awaited here (2026-10-12): it takes ~3s (measured), all it does is set
        # a flag read at the next tool call, and this turn has none left -- the reply
        # is ready. submit() starts the same check the same way.
        asyncio.ensure_future(self._check_permission_guard())
        log_event("engine", "small_model_turn_answered_finishing", tab_id=self.tab_id, text_len=len(text))
        self._reset_voice_narration()
        persisted_to = _append_small_model_turn_to_session(self.workspace_dir, self.tab_id, self.last_saved_session_id, question_text, text)
        if persisted_to:
            self.last_saved_session_id = persisted_to
            if _load_persisted_language(self.tab_id) is None:
                refresh_language_in_background(persisted_to, self.tab_id)
        recorder, self._sw_turn_recorder = self._sw_turn_recorder, None
        if recorder is not None and recorder.session_id == self.last_saved_session_id:
            answer = "" if silent else text
            try:
                sw_history.append_turn(self.workspace_dir, recorder.session_id, recorder.turn_id, question_text, answer, recorder.calls)
                self._sw_history_job = asyncio.create_task(sw_history.after_turn(
                    self.workspace_dir, recorder.session_id, self.tab_id, recorder, question_text, answer,
                ))
            except Exception as exc:  # noqa: BLE001 -- the reply itself must still go out
                log_event("engine", "sw_history_append_failed", tab_id=self.tab_id, error=str(exc))
        assistant_wire = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": "small-model",
                "stop_reason": "end_turn",
            },
            "session_id": self.last_saved_session_id,
            "parent_tool_use_id": None,
        }
        if not silent:
            # isVoice here too (2026-10-09): see _start_small_model_turn's turn_is_voice fix --
            # chat.js treats a turn as already spoken once a voiced narration remark of it
            # played, so without this the final answer itself would go unspoken.
            await self.send({"type": "sdk_message", "message": assistant_wire, "isVoice": is_voice})
            result_wire = {
                "type": "result",
                "subtype": "success",
                "duration_ms": 0,
                "is_error": False,
                "num_turns": 1,
                "session_id": self.last_saved_session_id,
                "total_cost_usd": 0.0,
                "result": text,
            }
            await self.send({"type": "sdk_message", "message": result_wire, "isVoice": is_voice})
        self.real_user_turn_answered = True
        self.last_visible_output_at = time.monotonic()
        self.consecutive_narration_count = 0
        clear_pending_turn(self.workspace_dir, self.tab_id)
        self.turn_pending = False
        self._end_small_model_turn()

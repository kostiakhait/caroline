from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class VoiceNarrationMixin:
    async def _synthesize_narration_audio(self, comment: str) -> str | None:
        """Same pipeline as main.py's "tts" control op, without its model call. None on
        any failure -- chat.js then synthesizes on its own, exactly as before.

        No model cleanup (2026-10-21): the narrator already writes numbers out in words
        (its prompt says so); the digits left are names from the request ("iPhone 17"),
        which the speech service reads as they are. Live 2026-10-17 that call cost a
        remark 9s of silence on a busy server."""
        from app.plugins.voice_api import strip_markup_for_speech, synthesize_speech, voice_for_gender

        try:
            cleaned = strip_markup_for_speech(comment)
            iso = {"Russian": "ru", "English": "en"}.get(current_language_name(self.tab_id))
            return await synthesize_speech(cleaned, voice_for_gender(get_persona_gender(self.workspace_dir)), language=iso)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_event("engine", "voice_narration_tts_failed", tab_id=self.tab_id, error=str(exc))
            return None
    async def _review_and_synthesize(self, comment: str, task: str) -> tuple[str, str | None]:
        """A voice-loop remark on its way out: reply.review (see VOICE_NARRATION_REVIEW)
        and its audio. Returns (the remark to say, its audio).

        The two run side by side: the review returns the remark unchanged far more often
        than not, and then the audio made meanwhile is the audio of the reviewed text.
        Only a remark the review did change is synthesized again. One after the other
        they cost the schedule their sum on every remark (narrator_scheduler_sim with
        REVIEW_S=3: longest gap 5.3s where the limit is 4.5s)."""
        if VOICE_NARRATION_REVIEW:
            started = time.monotonic()
            reviewed: str | None = None
            audio_as_drafted = asyncio.create_task(self._synthesize_narration_audio(comment))
            try:
                from app.plugins.voice_api import review_reply

                reviewed = await review_reply(comment, task=task, timeout=VOICE_NARRATION_REVIEW_TIMEOUT_S)
            except asyncio.CancelledError:
                audio_as_drafted.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 -- fail-open, as review_reply itself is
                log_event("engine", "review_reply_failed", tab_id=self.tab_id, error=str(exc))
            changed = bool(reviewed and reviewed != comment)
            log_event("engine", "voice_narration_reviewed", tab_id=self.tab_id, ms=int((time.monotonic() - started) * 1000),
                      answered=reviewed is not None, changed=changed)
            if not changed:
                return comment, await audio_as_drafted
            audio_as_drafted.cancel()
            comment = reviewed
        return comment, await self._synthesize_narration_audio(comment)
    def _voice_narration_applies(self) -> bool:
        """True while _voice_narration_loop owns narration: an sw-mode turn (the
        only path whose narrator sees real facts, see _narration_action_facts) that
        is a voice turn. _narration_turn_started_at is cleared the moment the final
        answer starts going out, which closes narration for the rest of the turn."""
        return self.small_model_active and self.turn_is_voice and self._narration_turn_started_at is not None
    def _reset_voice_narration(self) -> None:
        """Drops everything the voice loop holds for the current turn -- a drafted
        remark nobody has heard yet must never surface in a later turn."""
        self._narration_turn_seq += 1
        self._narration_turn_started_at = None
        if self._narration_draft_task is not None and not self._narration_draft_task.done():
            self._narration_draft_task.cancel()
        self._narration_draft_task = None
        if self._first_voice_remark_task is not None and not self._first_voice_remark_task.done():
            self._first_voice_remark_task.cancel()
        self._first_voice_remark_task = None
        self._first_voice_remark = None
        self._first_voice_remark_covers_start = False
        self._prepared_narration = None
        self._prepared_narration_is_first = False
        self._prepared_narration_stale = False
        self._stale_remarks_in_row = 0
        self._lines_since_answer_candidate = None
        self._held_for_answer = None
        self._narration_facts_at_last_draft = None
        self._narration_last_draft_at = None
        self._voice_narration_failed_at = None
        self.turn_narration_remarks = []
    def _note_answer_candidate(self) -> None:
        """A tier has an answer and it goes to the verifier now (run_small_model_turn's
        on_candidate). Counted from here by on_activity."""
        self._lines_since_answer_candidate = 0
    def _answer_being_checked(self) -> bool:
        """The verifier has the answer and nothing else has happened since: the reply is
        most likely seconds away. The check's own "Checking the result" is the one line it
        adds; anything more -- an escalation, a retry, a step down -- means the work goes on."""
        return self._lines_since_answer_candidate is not None and self._lines_since_answer_candidate <= 1
    def on_client_speech_state(self, speaking: bool) -> None:
        """chat.js's speech queue started an item (True) or ran dry (False) -- see its
        reportSpeechState. Wakes the loop so the next remark follows the last spoken
        line by VOICE_NARRATION_AFTER_SPEECH_GAP_S, not by up to a whole tick more."""
        was = self._client_speaking
        self._client_speaking = speaking
        self._client_speech_reported_at = time.monotonic()
        if not speaking and was is not False:
            self._client_quiet_since = time.monotonic()
        self._voice_narration_wake.set()
    def _client_quiet_for(self, now: float) -> float:
        """How long chat.js has had nothing to say. A client that never reported
        speech_state (older chat.js) gets an estimate: the last remark's length at an
        ordinary speaking rate, counted from when it was sent."""
        if self._client_speaking is not None:
            if self._client_speaking:
                reported = self._client_speech_reported_at
                if reported is not None and now - reported > VOICE_NARRATION_CLIENT_SPEAKING_STALE_S:
                    return now - reported - VOICE_NARRATION_CLIENT_SPEAKING_STALE_S
                return 0.0
            return now - self._client_quiet_since if self._client_quiet_since is not None else float("inf")
        if self.last_visible_output_at is None:
            return float("inf")
        return now - (self.last_visible_output_at + self._last_narration_est_speech_s)
    async def _voice_narration_loop(self) -> None:
        """Runs for the tab's lifetime; acts only while _voice_narration_applies().
        Each pass is quick (drafting runs in its own task), so a speech_state event is
        acted on as soon as it arrives."""
        try:
            while not self.ended:
                try:
                    await asyncio.wait_for(self._voice_narration_wake.wait(), timeout=VOICE_NARRATION_TICK_S)
                except asyncio.TimeoutError:
                    pass
                self._voice_narration_wake.clear()
                if self.ended:
                    return
                try:
                    await self._step_voice_narration()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 -- same rule as _watchdog_loop: log, keep ticking
                    log_event("engine", "voice_narration_tick_failed", tab_id=self.tab_id, error=str(exc), error_type=type(exc).__name__)
        except asyncio.CancelledError:
            pass
    def _stale_interval(self) -> float:
        """Silence before the next remark with nothing new -- see VOICE_NARRATION_STALE_STEP_S."""
        return min(VOICE_NARRATION_STALE_S + VOICE_NARRATION_STALE_STEP_S * self._stale_remarks_in_row, VOICE_NARRATION_STALE_MAX_S)
    async def _step_voice_narration(self) -> None:
        if not self._voice_narration_applies():
            return
        if not self.turn_pending or self.conn_state.get("kind") != "connected" or self.current_turn_suppress_narration:
            return
        now = time.monotonic()
        started = self._narration_turn_started_at or now

        # 1. A drafted remark goes out as soon as the client has finished speaking.
        if self._prepared_narration is not None:
            comment, drafted_at, audio = self._prepared_narration
            if now - drafted_at > VOICE_NARRATION_MAX_AGE_S:
                log_event("engine", "voice_narration_draft_expired", tab_id=self.tab_id, age_s=round(now - drafted_at, 1))
                self._prepared_narration = None
                self._prepared_narration_is_first = False
            elif self._answer_being_checked() and self._client_quiet_for(now) < VOICE_NARRATION_ANSWER_HOLD_S:
                if self._held_for_answer != comment:
                    self._held_for_answer = comment
                    log_event("engine", "voice_narration_held_for_answer", tab_id=self.tab_id, comment=comment)
                return
            elif (
                (self._prepared_narration_is_first or now - started >= VOICE_NARRATION_FIRST_S)
                and self._client_quiet_for(now) >= VOICE_NARRATION_AFTER_SPEECH_GAP_S
            ):
                self._stale_remarks_in_row = self._stale_remarks_in_row + 1 if self._prepared_narration_stale else 0
                self._prepared_narration = None
                self._prepared_narration_is_first = False
                self._prepared_narration_stale = False
                log_event("engine", "voice_narration_sent", tab_id=self.tab_id, comment=comment, waited_s=round(now - drafted_at, 1), turn_s=round(now - started, 1))
                await self._send_narration_remark(comment, audio)
                # The client reports speaking for this remark shortly; until then it
                # must not look quiet, or the next remark could be shown on top of it.
                if self._client_speaking is not None:
                    self._client_speaking = True
                    self._client_speech_reported_at = time.monotonic()
                return
            else:
                return

        # 2. Otherwise draft the next one ahead of time -- while the previous one is
        # still being spoken, so it is ready the moment the client goes quiet.
        if self._narration_draft_task is not None:
            return
        if now - started < VOICE_NARRATION_PREPARE_FROM_S:
            return
        if self._voice_narration_failed_at is not None and now - self._voice_narration_failed_at < VOICE_NARRATION_FAILURE_RETRY_S:
            return
        facts = self._narration_action_facts()
        if facts is None:
            return
        if self._first_voice_remark_covers_start:
            # Once per turn: the facts as they stand when the loop first looks say only
            # that the work has begun (the request was received, a tier is working), and
            # the remark the fast lane's decline came with says it (see
            # _say_first_voice_remark). They count as drafted; until a new fact, only the
            # stale rule below can speak.
            self._first_voice_remark_covers_start = False
            self._narration_facts_at_last_draft = facts
            self._narration_last_draft_at = now
        nothing_new = facts == self._narration_facts_at_last_draft
        seen = set((self._narration_facts_at_last_draft or "").splitlines())
        new_lines = [line for line in facts.splitlines() if line not in seen]
        if nothing_new:
            # Nothing new happened: at most one remark per VOICE_NARRATION_STALE_S,
            # counted from the later of the last draft and the end of the last speech.
            last = self._narration_last_draft_at or started
            wait = self._stale_interval() - VOICE_NARRATION_STALE_LEAD_S
            if now - last < wait or self._client_quiet_for(now) < wait:
                return
        self._narration_facts_at_last_draft = facts
        self._narration_last_draft_at = now
        # Elapsed time is a real fact too ("this one is taking a while"), but only for the
        # model: kept out of the comparison above, it would make every draft look new.
        facts_for_model = f"{facts}\nTime spent on the request so far: {_spoken_duration(now - started)}."
        if new_lines and len(new_lines) < len(facts.splitlines()):
            # Which lines are new is known here exactly; left to work it out from ALREADY
            # SAID, the model answered "nothing new" to a step that had just begun.
            facts_for_model += "\nNEW since your last remark:\n" + "\n".join(new_lines)
        if nothing_new:
            # Said outright (2026-10-11): left to judge "a long while" by the elapsed time
            # alone, the model stayed silent three drafts running, 21s on a live call.
            facts_for_model += (
                "\nNothing new has happened since the last remark, and the user has heard nothing for about "
                f"{int(self._stale_interval())} s."
            )
            if self._stale_remarks_in_row:
                # Data, not a new rule: how many "still working" remarks went before, so the
                # next one can say something else -- how long it has taken, where the work is.
                facts_for_model += (
                    f"\nRemarks in a row with nothing new so far: {self._stale_remarks_in_row} -- {VOICE_NARRATION_SAY_ELSE}"
                )
        already_said = list(self.turn_narration_remarks)
        if self._first_voice_remark is not None and self._first_voice_remark not in already_said:
            already_said.append(self._first_voice_remark)  # known already, even if not out yet
        self._narration_draft_task = asyncio.create_task(
            self._draft_voice_narration(self._narration_turn_seq, facts_for_model, self._narration_task(), already_said,
                                        stale=nothing_new)
        )
    async def _say_first_voice_remark(self, turn_seq: int, remark: str, audio_task: asyncio.Task[str | None] | None) -> None:
        """The remark the fast lane's decline came with (2026-10-11, see try_fast_answer):
        the model that read the request has already put into one sentence what the work
        is. Handed to the loop as a finished draft that needn't wait out
        VOICE_NARRATION_FIRST_S -- that wait is for turns that may answer within
        seconds, and a decline says this one won't. Live before this, the first remark
        was heard 12.9s after the question."""
        remark, audio = await audio_task if audio_task is not None else await self._review_and_synthesize(remark, self._narration_task())
        if turn_seq != self._narration_turn_seq or self._prepared_narration is not None or self.turn_narration_remarks:
            return  # the turn is over, or the loop has a remark of its own by now
        self._prepared_narration = (remark, time.monotonic(), audio)
        self._prepared_narration_is_first = True
        self._prepared_narration_stale = False
        log_event("engine", "voice_narration_drafted", tab_id=self.tab_id, comment=remark, first=True)
        self._voice_narration_wake.set()
    async def _draft_voice_narration(self, turn_seq: int, facts: str, task: str, already_said: list[str], stale: bool = False) -> None:
        try:
            await self._draft_voice_narration_body(turn_seq, facts, task, already_said, stale)
        finally:
            if turn_seq == self._narration_turn_seq:
                self._narration_draft_task = None
    async def _draft_voice_narration_body(self, turn_seq: int, facts: str, task: str, already_said: list[str], stale: bool = False) -> None:
        from app.plugins.voice_api import generate_progress_comment

        language = current_language_name(self.tab_id)
        comment: str | None = None
        for attempt in range(1, NARRATION_GENERATION_RETRY_ATTEMPTS + 1):
            try:
                # One call, drafted in the tab's language (2026-10-10): on the narrator golden
                # set 14/16 at 2.5s vs 11/16 at 7.3s for the SMALL-draft-plus-translation
                # route, with names kept right ("от Анны", not "от Anna") and no leaked
                # machinery. That route stays the fallback when this one can't run at all.
                # Any failure of the direct route -- even an unexpected exception -- must
                # still reach the fallback, not cost the whole attempt.
                try:
                    comment = await draft_narration_remark(
                        task, facts, already_said, language,
                        gender=get_persona_gender(self.workspace_dir), addressee_gender=get_owner_profile(self.workspace_dir).gender,
                        repeat_window=VOICE_NARRATION_STALE_REPEAT_WINDOW if stale else None,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log_event("engine", "voice_narration_direct_failed", tab_id=self.tab_id, error=str(exc), error_type=type(exc).__name__)
                    comment = None
                if comment is None:
                    comment = await generate_progress_comment(
                        "", language, timeout=NARRATION_NETWORK_TIMEOUT_S,
                        gender=get_persona_gender(self.workspace_dir), activity=facts, task=task,
                        addressee_gender=get_owner_profile(self.workspace_dir).gender, already_said=already_said,
                        retries=1,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log_event("engine", "voice_narration_draft_failed", tab_id=self.tab_id, attempt=attempt, error=str(exc))
                comment = None
            if comment or comment == "":
                break  # "" = nothing new to say (see generate_progress_comment), not a failure
            log_event("engine", "voice_narration_draft_retry", tab_id=self.tab_id, attempt=attempt, exhausted=attempt == NARRATION_GENERATION_RETRY_ATTEMPTS)
        if turn_seq != self._narration_turn_seq:
            # The turn ended (or a new one started) while drafting -- this remark is about a
            # state nobody is waiting on any more.
            log_event("engine", "voice_narration_draft_discarded", tab_id=self.tab_id, had_comment=bool(comment))
            return
        if comment == "" and stale and VOICE_NARRATION_SAY_ELSE not in facts:
            # Once more, told outright to say something else (2026-10-22): with nothing new
            # to tell, the model wrote "Я всё ещё ищу цены на iPhone 17 ..." six drafts
            # running, each rightly rejected as a repeat of "Ищу цены на iPhone 17 ...",
            # and the user heard nothing for 21.6s.
            log_event("engine", "voice_narration_stale_redraft", tab_id=self.tab_id)
            await self._draft_voice_narration_body(
                turn_seq, f"{facts}\nThat was already said, in other words -- {VOICE_NARRATION_SAY_ELSE}", task, already_said, stale,
            )
            return
        if comment == "":
            # Nothing new (or only a repeat). No cooldown needed: the next draft already
            # waits for new facts or VOICE_NARRATION_STALE_S, see _step_voice_narration.
            log_event("engine", "voice_narration_nothing_new", tab_id=self.tab_id)
            return
        if not comment:
            self._voice_narration_failed_at = time.monotonic()
            return
        self._voice_narration_failed_at = None
        # Synthesized here, while the previous remark is still playing (2026-10-10): seen
        # live, synthesis took 4-7s per remark -- every one of them dead air when the
        # client only asked for the audio once the remark had arrived.
        comment, audio = await self._review_and_synthesize(comment, task)
        if turn_seq != self._narration_turn_seq:
            log_event("engine", "voice_narration_draft_discarded", tab_id=self.tab_id, had_comment=True)
            return
        self._prepared_narration = (comment, time.monotonic(), audio)
        self._prepared_narration_stale = stale
        log_event("engine", "voice_narration_drafted", tab_id=self.tab_id, comment=comment)
        self._voice_narration_wake.set()

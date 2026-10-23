from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class TextNarrationMixin:
    def _narration_action_facts(self) -> str | None:
        """The only input the progress narrator is allowed to see: which
        actions are running right now and which finished this turn (ok or
        error). None when there are no actions at all -- the narrator then
        stays silent rather than invent anything from the conversation."""
        if self.small_model_active:
            return "\n".join(self.small_model_activity[-8:]) or None
        # Per-call lines, same "Tool call X (arguments: ...); result: ..." shape
        # small_model_engine.py's own emit() already produces for the small-model
        # path above -- the narrator prompt's few-shot examples (_progress_comment_
        # prompt) are tuned against exactly this shape and already instruct the
        # model to turn it into plain words without ever naming the machinery, so
        # reusing it here (2026-10-17) needed no prompt change. Before this, this
        # branch reduced everything to bare counts ("Steps in progress: 2") --
        # the narrator then had nothing concrete to vary between remarks and just
        # paraphrased the same vague sentence (confirmed live, see the screenshot
        # this fixes: 6 near-identical "proverila Upwork" remarks in a row).
        #
        # Bug fix (2026-10-11), confirmed live: every plugin tool call goes
        # through dispatch() (app/operations.py), which answers the SDK
        # almost immediately with {operation_id, status} while the real
        # work keeps running in the background -- _pending_tool_calls (raw
        # SDK ToolUseBlock tracking) empties out again within that same
        # instant, so a tool that was genuinely still running (a browser-
        # automation call, confirmed live at 148s+) looked like "nothing
        # running" to the narrator for its entire actual duration. REGISTRY
        # is the same per-tab ground truth ChatSession.stop()'s own
        # cancel_for_tab already relies on for "what's really running".
        running_calls = list(self._pending_tool_calls.values())
        running_agents = REGISTRY.running_for_tab(self.tab_id)
        finished = list(self.turn_tool_outcomes.values())
        if not running_calls and not running_agents and not finished:
            return None
        lines: list[str] = []
        for name, args_preview in running_calls:
            lines.append(f"Tool call {name} started (arguments: {args_preview}).")
        for op in running_agents:
            lines.append(f"Tool call {op.tool_name} is still running in the background.")
        for outcome in finished:
            result = "ERROR: " + outcome.result_preview if outcome.is_error else outcome.result_preview
            lines.append(f"Tool call {outcome.name} (arguments: {outcome.arguments_preview}); result: {result}")
        return "\n".join(lines)
    def _narration_task(self) -> str:
        """The user's request for the current task -- the one thing the narrator
        needs to say where the task stands. Only the current real user request,
        never older conversation history.

        Per explicit instruction (2026-10-13): a headless, bridge-enabled session
        (see approval_bridge_tab_id's own __init__ comment) never has a real user
        submit() of its own, so last_real_user_question would always be empty --
        this starved review_reply/check_permission_guard of any task context for
        EVERY background-task turn. pending_user_text is set unconditionally by
        every submit(), real or not (see submit()'s own assignment), so it's the
        right fallback specifically for a session that structurally never gets a
        real one."""
        if not self.last_real_user_question and self.approval_bridge_tab_id and self.pending_user_text:
            return self.pending_user_text.strip()[:600]
        # Bug fix (2026-10-20), confirmed live: a proactively-triggered turn
        # (pending_is_real_user=False -- a scheduled send, a background-op
        # completion nudge, etc.) has its OWN task, described by its own
        # pending_user_text -- but last_real_user_question only ever reflects
        # the last REAL conversational question, which can be about a
        # completely different, unrelated track still sitting there from
        # earlier. Confirmed live: review_reply, fed last_real_user_question
        # for an unrelated earlier Messenger investigation while reviewing a
        # proactive reply about a just-sent email, judged the (correct) email
        # reply "off-task" and rewrote it into fabricated Messenger-flavored
        # text. Only applies when the CURRENT turn itself isn't a real user
        # message -- a real user turn still always wants its own real
        # question, unchanged from before.
        if not self.pending_is_real_user and self.pending_user_text:
            return self.pending_user_text.strip()[:600]
        question = (self.last_real_user_question or "").strip()
        # A question that went out with interim notes in front (_with_interim_notes):
        # the narrator is told the user's own words, not the notes.
        if question.startswith(_INTERIM_NOTES_HEADER):
            cut = question.find("]\n\n")
            if cut != -1:
                question = question[cut + 3:].strip()
        return question[:600]
    def _narration_turn_marker(self) -> Any:
        """Identifies the turn a narration remark is being written for: the
        text that started it. A new turn always sets a new pending text."""
        return id(self.pending_user_text), self.pending_user_text
    async def _check_progress_narration(self) -> None:
        """Per explicit instruction (2026-09-10): the user must see SOME
        comment from Caroline at least once a minute while a real turn of
        hers is still running, even deep in a long tool-call chain --
        confirmed there is no safe way to inject anything into her own
        live session mid-turn to make that happen: the SDK's own input
        stream only ever delivers a newly-submitted message once the
        CURRENT turn (the whole tool-call chain) has fully finished (see
        _input_stream's own comment), and interrupt() is a genuine abort,
        not a "pause, say something, then resume" signal -- using it every
        minute would repeatedly disrupt real in-progress work. Sidesteps
        the problem entirely instead: a separate, lightweight ai:resolve
        call (generate_progress_comment, voice_api.py) drafts a short
        cosmetic stand-in remark, sent straight to the client. The real
        session never sees or knows about this. Repeats every
        PROGRESS_NARRATION_INTERVAL_MS as long as the turn keeps running;
        resets whenever the real model actually says something of its own
        (see the "assistant" wire-send site) or a real new user message
        starts a fresh turn."""
        # Bug fix (2026-09-10): confirmed live -- gating this on
        # last_real_user_question specifically meant a proactively-injected
        # turn (inject_proactive() always passes is_real_user=False --
        # startup greeting, reminders, ratatosk nudges, crash-resume) never
        # got narration at all on a fresh process, even a long one (e.g.
        # resuming an unfinished image-generation + document task after a
        # crash). _gather_recent_dialogue_for_narration now also falls back
        # to pending_user_text (set for every pending turn, real or
        # proactive), so the emptiness check below on the actual gathered
        # dialogue is the real, general guard now -- not this field.
        # (silent_turn removed 2026-09-11 -- whether the eventual reply
        # itself gets shown is now a per-message [[NO_UPDATE]] decision,
        # unrelated to whether this cosmetic aside narration comment fires.)
        #
        # Bug fix (2026-09-11), per explicit instruction: confirmed live --
        # turn_pending alone stays True for the ENTIRE duration of a rate-
        # limit-wait cycle (the turn is legitimately still "owed" a reply,
        # by design, so the retry can resume it later), even though nothing
        # is actually happening except quietly waiting for the limit to
        # reset. This kept firing narration every minute throughout that
        # wait, reacting to nothing real. Only narrate while actually
        # connected -- recovering/limited/restarting/billing_blocked all
        # mean there's genuinely nothing to narrate about right now.
        # Per explicit instruction (2026-10-13), "Фоновые задачи не должны иметь
        # нарратора": a headless session (see self.headless's own __init__
        # comment) has no chat window for a cosmetic stand-in remark to even
        # reach -- gated here architecturally, not just left to have nowhere to
        # go, so the paid generate_progress_comment call is never made for it at
        # all.
        if self.ended or not self.turn_pending or self.conn_state.get("kind") != "connected" or self.headless:
            return
        # sw-mode voice turns are _voice_narration_loop's (2026-10-09, see VOICE_NARRATION_TICK_S).
        if self._voice_narration_applies():
            return
        # Per explicit instruction (2026-10-07) -- NOT a revival of the
        # whole-category gate reverted just below on 2026-10-03: this is
        # narrower and opt-in per turn (current_turn_suppress_narration,
        # see its own __init__ comment), set only by scheduler-fired
        # reminders (main.py's _on_reminder_due, both "service" and "user"
        # event types -- see scheduler_plugin.py's EVENT_TYPES), not every
        # proactive turn. A real incident: the narrator was producing
        # mid-task "syncing"-style commentary for Caroline's OWN routine
        # reminders (a memory backup, a periodic mailbox check) -- chatter
        # about internal mechanics the user never asked to hear, on a
        # schedule Caroline set for herself. The FINAL reply still happens
        # as before (a service-type reminder stays silent via its own
        # [[NO_UPDATE]] instruction; a user-type one still reports the
        # outcome) -- only this cosmetic mid-task aside is suppressed.
        if self.current_turn_suppress_narration:
            return
        # Reverted (2026-10-03), explicit direct instruction: the 2026-09-15 gate
        # below used to skip narration entirely for a proactive/scheduled turn
        # (reminder, vault backup, startup greeting, ratatosk nudge -- anything
        # inject_proactive() starts with is_real_user=False), on the reasoning
        # that the user isn't watching one of those the way they watch a turn
        # they just sent. Overridden: narration's job is to keep ANY silence
        # during active work from reading as nothing happening, regardless of
        # who/what started that work -- a long-running proactive turn is still
        # a gap in communication if it goes quiet for a minute. The original
        # odd-hour-narration complaint this gate fixed (comments landing at
        # 12:26/1:51/1:59/2:00 AM) is a real tradeoff of this reversion, not
        # solved here -- if it recurs, that's the next thing to fix, not a
        # reason to bring this gate back silently.
        # Reverted (2026-10-03), explicit direct instruction ("не должно такого быть,
        # убрать"): this used to stop narration dead after MAX_CONSECUTIVE_NARRATION_
        # COMMENTS comments in a row, on the reasoning that a long-stuck turn wasn't
        # helped by yet another paraphrase of the same stale dialogue window. That
        # reasoning no longer fully applies now that narration is fed real, changing
        # facts (on_activity/emit -- see generate_progress_comment's own activity
        # param), not just a re-paraphrase of unchanging context -- and per the same
        # 2026-10-03 instruction that already reverted the proactive-turn gate above,
        # the narrator's job is to keep ANY silence during active work from reading as
        # nothing happening, for as long as the work actually takes, not just the
        # first few minutes of it.
        now = time.monotonic()
        if self.last_visible_output_at is not None and now - self.last_visible_output_at < PROGRESS_NARRATION_INTERVAL_MS / 1000:
            return
        # Bug fix (2026-09-22): a fully-failed attempt (see below) gets
        # its own short cooldown instead of re-passing the main 60s gate
        # above on literally the next 5s watchdog tick.
        if self.last_narration_failed_at is not None and now - self.last_narration_failed_at < NARRATION_FAILURE_RETRY_S:
            return
        # Bug fix (2026-09-22): last_visible_output_at is claimed AFTER a
        # successful send now (see below), not eagerly here -- confirmed
        # live the old eager claim let one slow generate+translate round-
        # trip (71s, real incident) silently cost an EXTRA clean 60s on
        # top of its own latency, since the interval was measured from the
        # ATTEMPT'S START rather than its actual completion. Re-entrancy
        # (the original reason given for the eager claim) isn't actually
        # possible here: _watchdog_loop awaits this whole coroutine
        # sequentially, one tick at a time -- no other tick can start
        # until this one returns, slow or not.
        from app.plugins.voice_api import generate_progress_comment, review_reply

        task = self._narration_task()
        dialogue = ""
        # Bug fix (2026-09-15), per explicit instruction ("Нарратор должен
        # давать сообщения раз в минуту"): confirmed live -- the SMALL
        # model backing generate_progress_comment can fail its own output
        # contract (echoing stale dialogue, truncated/malformed tags,
        # ignoring the format entirely) several times in a row (a real
        # incident: 3 straight failures before a 4th attempt succeeded).
        # Retry a few times in the SAME tick instead of waiting for the
        # next one. Each attempt also gets NARRATION_NETWORK_TIMEOUT_S
        # (2026-09-22, well under the default 30s-per-attempt SW API
        # budget -- see that constant's own comment for the 71s real
        # incident this fixes) so a slow/hung attempt fails fast and this
        # loop gets a real chance to try again within the SAME tick.
        # Narrator sees only real facts about a running small-model turn. No facts
        # -> stay silent (2026-10-03: filler without facts misleads the user).
        # Bug fix (2026-10-14), per explicit instruction: the 2026-10-12 "Нарратор
        # не имеет право молчать дольше минуты" fallback (a fixed "still working,
        # nothing new" line, sent in place of a real comment whenever a minute
        # passed with no concrete tool activity to report) was meant for a
        # genuinely STUCK turn looking dead for minutes on end -- but it fired on
        # every ordinary no-activity minute too (the model just thinking/
        # generating, no tool calls), turning into repeated, content-free spam.
        # That rule was only ever meant to cover active work, not silence while
        # nothing is actually running -- reverted to simply staying silent here;
        # a genuinely hung turn is check_hang_tick's own job to detect and
        # recover from, not this cosmetic narrator's.
        activity = self._narration_action_facts()
        if activity is None:
            log_event("engine", "progress_narration_skipped_no_facts", tab_id=self.tab_id)
            return
        # Ported from a teammate's branch (807b3ea): how long this request has
        # been running is a real fact too ("this one is taking a while"), but
        # kept OUT of `activity` itself (which staleness/other checks compare
        # against) -- a separate string just for the model, built fresh each
        # attempt below isn't needed since elapsed time isn't part of any
        # equality check here.
        elapsed_s = int(time.monotonic() - self.turn_pending_since) if self.turn_pending_since is not None else None
        activity_for_model = activity if elapsed_s is None else f"{activity}\nTime spent on this request so far: {elapsed_s}s."
        # Bug fix (2026-10-09), confirmed live: everything above is checked
        # BEFORE the remark is generated, and generating it (a network call,
        # retried) takes up to a minute. In that time the turn can finish and
        # the model's own final report go out -- and the stale remark was then
        # sent after it ("нарратор вылезает за пределы финального доклада":
        # 4 of 13 remarks in one evening landed 30-66 s after their turn had
        # ended). The remark is a stand-in for silence DURING a turn, so it is
        # only sent if, once it is ready, this is still the same silent turn.
        silence_marker = self.last_visible_output_at
        turn_marker = self._narration_turn_marker()
        stop_epoch = self._narration_stop_epoch

        def _stale() -> str | None:
            if self.ended or not self.turn_pending:
                return "turn_ended"
            # A stop (user Stop or permission-guard stop, both go through
            # stop()) can land, kill the engine and start a fresh turn whose
            # pending_user_text/turn_pending end up looking just like the one
            # being narrated here -- the epoch check catches that case even
            # when the marker/silence checks above don't. See
            # _narration_stop_epoch's own __init__ comment.
            if self._narration_stop_epoch != stop_epoch:
                return "stopped"
            if self._narration_turn_marker() != turn_marker:
                return "another_turn"
            if self.last_visible_output_at != silence_marker:
                return "model_spoke"
            if self.current_turn_suppress_narration:
                return "narration_suppressed"
            return None

        comment: str | None = None
        for attempt in range(1, NARRATION_GENERATION_RETRY_ATTEMPTS + 1):
            if _stale():
                break
            try:
                comment = await generate_progress_comment(
                    dialogue, current_language_name(self.tab_id), timeout=NARRATION_NETWORK_TIMEOUT_S,
                    gender=get_persona_gender(self.workspace_dir), activity=activity_for_model, task=task,
                    addressee_gender=get_owner_profile(self.workspace_dir).gender,
                    already_said=list(self.turn_narration_remarks), retries=1,
                )
            except Exception as exc:
                log_event("engine", "progress_narration_failed", tab_id=self.tab_id, attempt=attempt, error=str(exc))
                comment = None
            if comment:
                break
            log_event("engine", "progress_narration_retry", tab_id=self.tab_id, attempt=attempt, exhausted=attempt == NARRATION_GENERATION_RETRY_ATTEMPTS)
        stale = _stale()
        if stale:
            log_event("engine", "progress_narration_dropped_stale", tab_id=self.tab_id, reason=stale, had_comment=bool(comment))
            return
        if not comment:
            self.last_narration_failed_at = time.monotonic()
            return
        self.last_narration_failed_at = None
        # Per explicit instruction (2026-10-11): reply.review checks this
        # already-translated remark for task relevance, internal-mechanics
        # leaks, and human-sounding tone -- fail-open to `comment` as is on
        # any failure, same philosophy as translate_text above.
        try:
            reviewed = await review_reply(comment, task=task)
        except Exception as exc:
            log_event("engine", "review_reply_failed", tab_id=self.tab_id, error=str(exc))
            reviewed = None
        if reviewed and reviewed != comment:
            log_event(
                "engine", "review_reply_applied", tab_id=self.tab_id,
                original_len=len(comment), reviewed_len=len(reviewed),
                original_text=comment, reviewed_text=reviewed,
            )
            comment = reviewed
            # Per explicit instruction (2026-10-13): generate_progress_comment
            # produces `comment` already in the target language (passed
            # straight to that call, unlike _translate_wire_visible_text's own
            # separate translate step) -- but review's OWN rewrite is not
            # guaranteed to preserve that language (same root cause as that
            # function's own 2026-10-13 fix: review_reply's LLM defaults to
            # English when rewriting). Only worth a translate pass when review
            # actually changed something -- re-translating an unchanged
            # comment would just be wasted cost.
            try:
                from app.plugins.voice_api import translate_text
                # Per explicit instruction (2026-10-15): same narration-is-
                # cosmetic-filler reasoning as generate_progress_comment's own
                # NARRATION_NETWORK_TIMEOUT_S/retries=1 just above -- this runs
                # right after it, still under the same 60s promise, and used to
                # default to a 30s timeout with 3 INTERNAL retries (up to ~91s
                # on its own, on top of everything else this tick already
                # spent) with nothing accounting for that hidden cost.
                translated = await translate_text(
                    comment, current_language_name(self.tab_id), gender=get_persona_gender(self.workspace_dir),
                    addressee_gender=get_owner_profile(self.workspace_dir).gender, context=task,
                    timeout=NARRATION_NETWORK_TIMEOUT_S, retries=1,
                )
            except Exception as exc:
                log_event("engine", "wire_translate_failed", tab_id=self.tab_id, error=str(exc))
                translated = None
            if translated and translated != comment:
                log_event(
                    "engine", "wire_translate_applied", tab_id=self.tab_id, language=current_language_name(self.tab_id),
                    original_len=len(comment), translated_len=len(translated),
                    original_text=comment, translated_text=translated,
                )
                comment = translated
        # Per explicit instruction (2026-10-13): review_reply can itself decide
        # this remark is pure internal-mechanics (or off-task) and rewrite it
        # down to the [[NO_UPDATE]] sentinel or a known silent-reply paraphrase
        # -- see _translate_wire_visible_text's own docstring for the real
        # incident this same check fixes there. Treated the same way here:
        # the narration attempt is still consumed (state above already
        # updated), just nothing is actually shown.
        if _NO_UPDATE_SENTINEL in comment or _is_silent_reply_paraphrase(comment):
            log_event("engine", "review_reply_produced_no_update", tab_id=self.tab_id, text=comment)
            return
        # Bug fix (2026-10-14), confirmed live ("Нарратор работает после
        # окончательного ответа"): the one _stale() check earlier (right after
        # generate_progress_comment returns) left review_reply/translate_text
        # above and _check_permission_guard below unguarded -- each is its own
        # real network round trip, and the turn can finish DURING any of them
        # just as easily as during generation itself. A real incident: the
        # turn (and its own silent [[NO_UPDATE]] conclusion) had already ended
        # by the time review_reply alone finished, and the stale "Всё ещё
        # проверяю..." comment still reached the user's chat ~2s later, with
        # no turn left for it to be commentary on. Re-checked here, before
        # anything else runs or gets recorded as having been said.
        stale = _stale()
        if stale:
            log_event("engine", "progress_narration_dropped_stale", tab_id=self.tab_id, reason=stale, stage="pre_send")
            return
        # No permission-guard re-check here (2026-10-22, explicit instruction): a
        # progress remark asks nothing and seeks no approval, and a check run at
        # one armed a block on the sw test box -- "a new recipient needs approval",
        # in the middle of a send the user had ordered -- that stopped the turn at
        # its next send. Real answers and the user's own messages still re-check.
        await self._send_narration_remark(comment)
    async def _send_narration_remark(self, comment: str, audio_base64: str | None = None) -> None:
        """Shows one narration remark -- shared by the 60s path above and the voice
        loop. isNarration (2026-10-09) lets chat.js drop a remark still waiting in its
        speech queue once the real answer arrives, instead of reading it out after."""
        self.last_visible_output_at = time.monotonic()
        # Kept so the NEXT draft in this same episode sees its own earlier remarks.
        self.turn_narration_remarks.append(comment)
        del self.turn_narration_remarks[:-20]
        self._last_narration_est_speech_s = len(comment) / VOICE_NARRATION_EST_CHARS_PER_S
        self.consecutive_narration_count += 1
        log_event("engine", "progress_narration_sent", tab_id=self.tab_id, comment=comment, consecutive_count=self.consecutive_narration_count)
        # What the user just saw must be something Caroline knows she said. Kept
        # until the next submitted message, where it's handed to the model as a
        # clearly labeled interim note -- nothing here starts a turn of its own.
        self.pending_interim_notes.append(comment)
        wire = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": comment}], "model": None, "stop_reason": None},
            "session_id": None, "parent_tool_use_id": None,
        }
        # Bug fix (2026-09-27), confirmed live ("сообщения нарратора вообще не
        # озвучиваются"): every other send site attaches "isVoice" at the top level of
        # this envelope (see _push_message/the ResultMessage handler) -- chat.js's own
        # auto-speak gate (playOneSpeech's caller) fires ONLY when evt.isVoice is truthy;
        # missing here meant it was always undefined for a narration message, so it never
        # auto-spoke, in Visual Mode or otherwise, regardless of whether the turn it's
        # narrating about was itself voice-started. Uses self.turn_is_voice, same as the
        # other real send sites, so narration follows the SAME voice/silent choice the
        # turn it's commenting on already made.
        envelope: dict[str, Any] = {"type": "sdk_message", "message": wire, "isVoice": self.turn_is_voice, "isNarration": True}
        if audio_base64:
            envelope["audioBase64"] = audio_base64
        await self.send(envelope)

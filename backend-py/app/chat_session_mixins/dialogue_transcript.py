from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class DialogueTranscriptMixin:
    def recent_real_user_directives_text(self, within_hours: float = 1.0) -> str:
        """Per explicit instruction (2026-10-13): the approval-bridge accessor --
        see recent_real_user_messages' own __init__ comment. Registered (by tab
        id) via session_context.register_recent_real_user_directives in
        _run_loop, so a DIFFERENT (headless) session's own permission-guard check
        can reach this tab's recent real approvals without importing main.py."""
        cutoff = time.time() * 1000 - within_hours * 3600_000
        lines = [m for t, m in self.recent_real_user_messages if t >= cutoff]
        if not lines:
            return ""
        return "[Recent real user messages/approvals from the primary conversation, last hour:]\n" + "\n".join(
            f"- {m}" for m in lines
        )
    def _gather_recent_dialogue_for_narration(self, limit: int = RECENT_DIALOGUE_WINDOW) -> str:
        """Per explicit correction (2026-09-10): the first version of
        _check_progress_narration fed generate_progress_comment only the
        single original question plus a list of currently-running tool
        names -- confirmed live (screenshots), this produced bland,
        near-identical, operation-sounding remarks every time ("I'm
        checking the phone-related messages...", "let me pull up that
        operation...") instead of anything that actually engaged with the
        conversation, and stayed generic turn after turn since none of its
        inputs ever changed mid-turn. Rebuilt to hand the model the REAL
        recent back-and-forth instead, via _read_recent_dialogue_lines --
        shared with refresh_language_in_background (see that module-level
        function's own docstring for why this used to be two separately-
        drifting readers, and why the window widened from 8/5 to
        RECENT_DIALOGUE_WINDOW).

        Per explicit instruction (2026-09-12): the narrator must work in
        BOTH engines. A small-model turn (small_model_engine.py) never
        talks to the CLI at all, so nothing it does ever reaches the
        on-disk transcript _read_recent_dialogue_lines reads below --
        while one is active, prefer its own live in-memory exchange
        instead (small_model_live_dialogue, kept current by
        on_live_dialogue_update as the turn progresses)."""
        if self.small_model_active and self.small_model_live_dialogue:
            return "\n".join(self.small_model_live_dialogue[-limit:])

        # Bug fix (2026-09-16), per explicit instruction: "нарратор должен
        # комментировать только текущую задачу, а не весь предыдущий
        # диалог" -- bounded to the CURRENT real user turn's own start
        # (see last_real_user_turn_started_at_ms's own comment), not just
        # a blind last-`limit`-lines window that could still reach back
        # into an earlier, already-finished task if the current one hasn't
        # produced `limit` lines of its own yet. None (no real user turn
        # started yet this process lifetime) keeps the old unbounded
        # behavior -- nothing to anchor to.
        lines = _read_recent_dialogue_lines(
            self.last_saved_session_id, self.tab_id, self.workspace_dir, limit,
            min_ts_ms=self.last_real_user_turn_started_at_ms,
        )

        # Bug fix (2026-09-15), per explicit instruction ("внимательно
        # смотри... почему он возвращает херню"): confirmed live -- a
        # tab that repeatedly hit the same usage-limit/error condition can
        # have the SAME assistant line (e.g. "You've hit your session
        # limit...") land in the transcript several times in a row (each
        # one a real reply the user actually saw at the time, so it's not
        # synthetic-marked and doesn't get filtered as such) -- confirmed
        # live: 5 of a 12-line window were the identical repeated line.
        # That dominates the "recent conversation" the narrator is asked
        # to react to, and directly produced one of its failures (echoing
        # that exact line back). Collapse consecutive exact repeats to one
        # occurrence here -- narrator-specific (this function's own
        # output), not in the shared _read_recent_dialogue_lines/
        # _usable_dialogue_lines readers, which other consumers (language
        # detection, get_history) may have their own reasons to keep as-is.
        deduped: list[str] = []
        for line in lines:
            if not deduped or deduped[-1] != line:
                deduped.append(line)
        lines = deduped

        # Bug fix (2026-09-10): confirmed live -- the disk-persisted
        # transcript doesn't yet contain a real user message that's merely
        # QUEUED (submitted but not yet consumed/flushed by the CLI, e.g.
        # because this tab's query() is still stuck resuming). The
        # narrator then only ever saw Caroline's OWN earlier turn and
        # reacted to nothing the user actually just said -- reading as if
        # she'd lost track of their answer entirely. Always make sure the
        # most recent real question is represented as the LAST line,
        # appending it if the disk read didn't already surface it.
        # Also (2026-09-10): last_real_user_question is only ever set by a
        # REAL submit() (is_real_user=True). pending_user_text used to be
        # used as a blanket fallback for ANY pending turn, real or
        # proactive, so a long-running proactive task still had something
        # to narrate about before anything hit the real transcript on disk.
        #
        # Bug fix (2026-09-11), per explicit instruction: confirmed live --
        # that blanket fallback also caught purely-internal synthetic
        # nudges (the API-retry recheck: "[Internal: automatic recheck
        # after an API/subscription limit blocked a previous turn.]
        # Continue any unfinished work... Reply in English."), labeled them
        # "User: ..." same as real speech, and fed that straight into the
        # narrator -- which then both reacted to internal bookkeeping as if
        # it were conversation AND picked up the nudge's own hardcoded
        # English wording, breaking language for an otherwise-Russian
        # conversation. Only trust pending_user_text here when it's a REAL
        # user's own words (pending_is_real_user, set alongside it in
        # submit()) -- a genuinely informative proactive task (companion
        # message, startup greeting) loses this one fallback line for the
        # brief window before anything real lands on disk, but
        # _read_recent_dialogue_lines (the real, general source) picks it
        # up the moment it does; that's a far smaller gap than actively
        # narrating on internal nudge text for the whole length of a
        # rate-limit wait.
        question = (self.last_real_user_question or (self.pending_user_text if self.pending_is_real_user else None) or "").strip()
        if question and (not lines or question not in lines[-1]):
            lines.append(f"User: {question}")

        return "\n".join(lines[-limit:])
    async def _translate_wire_visible_text(self, wire: dict[str, Any]) -> dict[str, Any] | None:
        """Per explicit instruction (2026-09-15): forced translation used
        to be narration-only (generate_progress_comment) -- confirmed live
        this left a real gap: the real Claude model itself dropped a terse
        English status line ("Now executing deletes and marks in
        batches.") into an otherwise-Russian conversation mid-tool-call-
        chain, and language_hint_instruction's system-prompt nudge is
        advisory, not a guarantee. Runs every REAL visible assistant text
        block through the same translate_text() forced-correction pass
        narration already uses, targeting current_language_name(tab_id).
        Falls back to the ORIGINAL text per block on any translate/review
        failure -- never blocks or drops a real reply over an
        infrastructure hiccup. Only touches "assistant" wire messages;
        "result"/"system"/etc. are left exactly as they were.

        Per explicit instruction (2026-10-13): reply.review now runs
        BEFORE translate, not after. Confirmed live: reviewing already-
        translated text produced broken, often English, output
        disconnected from what was actually said -- reply.review's own
        LLM defaults to English when asked to rewrite non-English input
        it has no instruction to preserve the language of. Real incident:
        a correctly-Russian status line about the vault-note timestamp
        came back from review as "Notes available. Current
        persona/reminders saved to Caroline:Vault." -- unrelated-sounding
        English boilerplate. Running translate on WHATEVER review decides
        to keep means the final thing shown is always in the right
        language, including review's own rewrites.

        review_reply can itself decide a block is pure internal-mechanics
        (or off-task) and rewrite it down to the [[NO_UPDATE]] sentinel or
        a known silent-reply paraphrase -- the same incident's reviewed
        text literally ended ". [[NO_UPDATE]]" on one occurrence. That was
        never checked for afterward, so a would-be-silent rewrite just got
        translated and shown anyway. Per explicit instruction (2026-10-13:
        "Модель должна докладывать промежуточные итоги как и раньше, но
        микроагентный фильтр должен отфильтровывать служебные сообщения"):
        this is the filter doing exactly that, per block, not a blanket
        suppression of intermediate replies -- a block review clears goes
        out completely normally. A block review flags this way is dropped
        from the wire entirely here, same as _strip_no_update_from_wire
        already does for the model's OWN [[NO_UPDATE]] replies; if that
        empties the message of everything worth showing, returns None --
        callers must check for that the same way they already do for
        _strip_no_update_from_wire's own result."""
        if wire.get("type") != "assistant":
            return wire
        content_blocks = wire.get("message", {}).get("content") or []
        if not any(isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip() for b in content_blocks):
            return wire
        from app.plugins.voice_api import review_reply, translate_text

        language = current_language_name(self.tab_id)
        # Per explicit instruction (2026-10-12): _narration_task()
        # (last_real_user_question), not self.pending_user_text -- that goes
        # empty/stale deep into a long multi-step turn (it tracks the CURRENT
        # turn's own text, not the standing task). A real incident showed
        # reply.review silently passing a raw tool-result JSON blob straight
        # through because it got task="" and had nothing to judge relevance
        # against. Same stable task source every other review/guard call site
        # here already uses.
        task = self._narration_task()
        dropped: list[dict[str, Any]] = []
        # Per explicit instruction (2026-10-15, "Кэролайн ДОЛЖНА ЗНАТЬ о том,
        # какой текст отображается пользователю"): review_reply/translate_text
        # below mutate block["text"] for what the USER sees, but the CLI engine
        # (Claude Code or Codex -- see _patch_cli_transcript_text's own doc
        # comment) already wrote its OWN, pre-mutation version of this exact
        # text to its own on-disk session transcript by the time this runs --
        # that file, not this in-memory wire, is what a resumed session reads
        # back as "what I said". Collected here, patched once after the loop.
        transcript_patches: list[tuple[str, str]] = []
        for block in content_blocks:
            if not (isinstance(block, dict) and block.get("type") == "text"):
                continue
            text = block.get("text") or ""
            if not text.strip():
                continue
            original_text = text
            try:
                reviewed = await review_reply(text, task=task)
            except Exception as exc:
                log_event("engine", "review_reply_failed", tab_id=self.tab_id, error=str(exc))
                reviewed = None
            if reviewed and reviewed != text:
                # Bug fix (2026-10-13), per explicit instruction: unlike
                # translate_text's own wire_translate_applied, a review
                # rewrite that actually CHANGED the text was never logged
                # at all -- only its failures were (review_reply_failed).
                # review_reply's whole job is to rewrite/shorten a reply it
                # judges off-task or leak-like; with no record of what it
                # changed, a real reply the model generated (and still
                # believes it sent, since this runs after the model's own
                # turn finished) could be silently shortened or swapped for
                # something unrecognizable here, with nothing in the log to
                # show it happened -- exactly the kind of "something ate
                # the output" gap this is meant to close.
                log_event(
                    "engine", "review_reply_applied", tab_id=self.tab_id,
                    original_len=len(text), reviewed_len=len(reviewed),
                    original_text=text, reviewed_text=reviewed,
                )
                text = reviewed
                block["text"] = text
            is_silent = _NO_UPDATE_SENTINEL in text or _is_silent_reply_paraphrase(text)
            # Per explicit instruction (2026-10-13): current_language_name(tab_id)
            # is only ever written by refresh_language_in_background, called only
            # from a REAL user's own submit() -- a headless session (see
            # self.headless's own __init__ comment) never has one, so translating
            # here would permanently stick at the hardcoded English fallback.
            # review_reply above still runs (it keeps filtering internal-mechanics
            # leaks before anything reaches the relay buffer) -- only the
            # translate step is skipped; the one real translation happens once,
            # under the RECEIVING tab's own real language, in deliver_relayed_reply.
            if not is_silent and not self.headless:
                try:
                    translated = await translate_text(
                        text, language, gender=get_persona_gender(self.workspace_dir),
                        addressee_gender=get_owner_profile(self.workspace_dir).gender, context=task,
                    )
                except Exception as exc:
                    log_event("engine", "wire_translate_failed", tab_id=self.tab_id, error=str(exc))
                    translated = None
                if translated and translated != text:
                    # Per explicit instruction (2026-10-13): lengths alone
                    # don't say whether a translation actually preserved the
                    # content -- the real before/after text is what's needed
                    # to tell a legitimate (if shorter/longer) translation
                    # apart from one that silently dropped or mangled part of
                    # a real reply the model generated and believes it sent.
                    log_event(
                        "engine", "wire_translate_applied", tab_id=self.tab_id, language=language,
                        original_len=len(text), translated_len=len(translated),
                        original_text=text, translated_text=translated,
                    )
                    block["text"] = translated
            # Per explicit instruction (2026-10-11): re-evaluate the permission
            # guard on every outbound message -- see _check_permission_guard's
            # own docstring for why this runs here too, not just in submit().
            await self._check_permission_guard()
            # Per explicit instruction (2026-10-20), after a real incident:
            # a clarifying question the user already answered earlier in the
            # dialogue must never be asked again -- see
            # _check_question_already_answered's own docstring. Checked
            # BEFORE delivery-completeness (and suppresses it if it fires):
            # a message about to be hidden from the user has nothing worth
            # checking for completeness.
            if not is_silent and await self._check_question_already_answered(text):
                is_silent = True
            # Per explicit instruction (2026-10-18), after a real incident:
            # also check that THIS message actually delivers everything it
            # claims to -- see _check_delivery_completeness's own docstring.
            # Skipped for a silent/dropped block -- nothing is actually
            # reaching the user, so there is nothing to check.
            if not is_silent:
                await self._check_delivery_completeness(text)
            if is_silent:
                log_event("engine", "review_reply_produced_no_update", tab_id=self.tab_id, text=text)
                dropped.append(block)
            elif block["text"] != original_text:
                transcript_patches.append((original_text, block["text"]))
        if transcript_patches:
            await self._patch_cli_transcript_text(transcript_patches)
        if dropped:
            kept = [b for b in content_blocks if b not in dropped]
            if not any(isinstance(b, dict) and b.get("type") in ("text", "tool_use") for b in kept):
                log_event("engine", "wire_dropped_entirely_after_review", tab_id=self.tab_id)
                return None
            wire["message"]["content"] = kept
        return wire
    async def _check_question_already_answered(self, final_text: str) -> bool:
        """Per explicit instruction (2026-10-20), after a real incident:
        Caroline asked which Facebook account to connect, the user
        answered, and when they later said "go ahead", she asked the exact
        same question again from scratch -- the earlier answer was right
        there in the dialogue the whole time, unused. Backed by a new,
        client-only microagent (turn.question.already_answered, app/
        microagents/question_already_answered.py -- see that module's own
        docstring; never deployed to Camerlengo, same as delivery_
        completeness.py/permission_identify.py).

        Unlike _check_delivery_completeness (which never blocks the send,
        only corrects afterward), a positive result HERE returns True so
        the caller suppresses this message outright -- the user must never
        see a question they already answered, not even once more before a
        correction could land. Also inject_proactive()s the found answer as
        an internal note, same non-blocking follow-up pattern
        _on_permission_block_checked already uses, so the model picks the
        turn back up with the answer in hand instead of just going silent.

        Fail-open on any error: False (never already answered) -- an
        infrastructure hiccup must never itself suppress a real question
        the user hasn't actually answered yet."""
        from app.plugins.voice_api import check_question_already_answered

        dialogue = self._gather_recent_dialogue_for_permission_guard()
        try:
            result = await check_question_already_answered(final_text, dialogue)
        except Exception as exc:
            log_event("engine", "check_question_already_answered_failed", tab_id=self.tab_id, error=str(exc))
            return False
        if result is None or not result["already_answered"]:
            return False
        answer = result["answer"]
        log_event("engine", "question_already_answered_suppressed", tab_id=self.tab_id, answer=answer, suppressed_text=final_text)
        self.inject_proactive(
            f"[Internal: you just started to ask the user a question they already answered earlier in this "
            f"conversation -- {answer} That question was suppressed, the user never saw it. Proceed using "
            f"that answer now; do not ask it again.]"
        )
        return True
    async def _check_delivery_completeness(self, final_text: str) -> None:
        """Per explicit instruction (2026-10-18), after a real incident:
        Caroline narrated having shown the user a Chinese email draft for
        approval, when the actual outbound message never contained it --
        her own belief about what she delivered diverged from what was
        actually sent. Backed by a new, client-only microagent (turn.
        delivery.completeness, app/microagents/delivery_completeness.py --
        see that module's own docstring; never deployed to Camerlengo, same
        as every other new agent this task added).

        `final_text` is the REAL final text, after review_reply/
        translate_text have already run -- what the user is actually about
        to see, not what the model originally drafted.

        Never blocks the send itself (confirmed scope: every outbound
        message, not just action-tied ones) -- on an incomplete delivery,
        inject_proactive()s a corrective note describing what's missing,
        same non-blocking pattern _on_permission_block_checked already uses
        for a permission block. Fail-open on any error: None (or a failed
        call) is treated as complete, never itself a reason to interrupt
        the user with a false alarm."""
        from app.plugins.voice_api import check_delivery_completeness

        dialogue = self._gather_recent_dialogue_for_permission_guard()
        try:
            result = await check_delivery_completeness(final_text, dialogue)
        except Exception as exc:
            log_event("engine", "check_delivery_completeness_failed", tab_id=self.tab_id, error=str(exc))
            return
        if result is None or result["complete"]:
            return
        missing = result["missing"]
        log_event("engine", "delivery_incomplete", tab_id=self.tab_id, missing=missing)
        self.inject_proactive(
            f"[Internal: the last message you sent did not actually deliver everything it claimed to -- "
            f"{missing} Please actually include it now.]"
        )
    async def _patch_cli_transcript_text(self, replacements: list[tuple[str, str]]) -> None:
        """Rewrites the CLI engine's own just-written transcript entry so its
        text matches what the user actually saw, not what the model originally
        said before review_reply/translate_text changed it.

        Per explicit instruction (2026-10-15, confirmed live): the user
        corrected a reply that had come out partly in English; Caroline flatly
        denied it ("the text is already fully in Russian... if you still see
        English, that's a display issue on your side"), because she was right
        about what SHE said and had no way to know a later pipeline step
        changed it before it reached the screen. Her only memory of "what I
        said" is this session's own transcript file, which the CLI process
        (Claude Code or Codex -- both write their own session .jsonl
        independently of this process; see durability.py's session_transcript_
        path, which already resolves to whichever of the two actually holds
        this session) wrote BEFORE review/translate ever ran. Applies equally
        to both CLI engines -- same file shape, same gap, same fix; the
        small-model engine needs none of this, since _append_small_model_
        turn_to_session (this module) already writes the FINAL, already-
        translated text itself, there being no separate CLI process for it to
        diverge from in the first place.

        Content-matched against the LAST transcript entry, not uuid-matched:
        the Python SDK's own AssistantMessage dataclass (see wire.py's
        message_to_wire) carries no uuid/parentUuid at all -- those are
        CLI-transcript-only fields the SDK never exposes to this process. If
        the last entry's text doesn't match what we expect to replace (a
        race, an unexpected ordering), this leaves the file untouched rather
        than risk corrupting the wrong entry. Best-effort: logged, never
        raised -- a missed patch here is a history-accuracy gap, not a reason
        to break a real reply already on its way to the user."""
        if not self.last_saved_session_id:
            return
        path = session_transcript_path(self.workspace_dir, self.last_saved_session_id)
        if not path.exists():
            return
        try:
            raw_text = path.read_text(encoding="utf-8")
        except Exception as exc:
            log_event("engine", "transcript_patch_read_failed", tab_id=self.tab_id, error=str(exc))
            return
        lines = raw_text.splitlines()
        for i in range(len(lines) - 1, -1, -1):
            if not lines[i].strip():
                continue
            try:
                entry = json.loads(lines[i])
            except Exception:
                log_event("engine", "transcript_patch_unparseable_last_line", tab_id=self.tab_id)
                return
            if entry.get("type") != "assistant":
                log_event("engine", "transcript_patch_last_entry_not_assistant", tab_id=self.tab_id, entry_type=entry.get("type"))
                return
            content = (entry.get("message") or {}).get("content") or []
            patched_any = False
            for original_text, final_text in replacements:
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text") == original_text:
                        block["text"] = final_text
                        patched_any = True
            if not patched_any:
                log_event("engine", "transcript_patch_no_match", tab_id=self.tab_id)
                return
            lines[i] = json.dumps(entry, ensure_ascii=False)
            try:
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except Exception as exc:
                log_event("engine", "transcript_patch_write_failed", tab_id=self.tab_id, error=str(exc))
                return
            log_event("engine", "transcript_patched", tab_id=self.tab_id, replacement_count=len(replacements))
            return
        log_event("engine", "transcript_patch_empty_file", tab_id=self.tab_id)
    async def deliver_relayed_reply(self, text: str, task: str | None = None, is_voice: bool = False) -> None:
        """Per explicit instruction (2026-10-13), "Фоновые задачи... должны
        мержиться только на уровне вывода пользователю": delivers a reply that
        was generated and already reviewed inside a DIFFERENT (headless)
        session -- e.g. a real tab's own pseudo-session (app/pseudo_tab.py)
        final answer, relayed here once that session's turn genuinely ends --
        into THIS session's own visible chat,
        as if it were this session's own reply. Same bypass-the-model,
        translate-then-send-as-wire pattern as _send_permission_guard_direct_
        ask. review_reply already ran inside the
        ORIGINATING headless session (see _translate_wire_visible_text's own
        headless branch) -- only translation into THIS tab's own real language
        happens here, since the headless session that generated `text` has no
        real language of its own to translate into.

        `task` (2026-10-15): whatever `text` is actually supposed to be a
        result OF (a reminder's own note, or a sub-agent's own brief) --
        passed straight through to check_background_relay_worth so it can
        judge the result against the task, not in isolation.

        Bug fix (2026-10-14), confirmed live: review_reply only filters
        internal-mechanics leaks/off-task/robotic tone -- it has no opinion
        on whether `text` is worth this interruption at all, and three of
        four messages relayed one morning were routine "nothing happened"
        status noise that passed review_reply untouched (it's clean,
        on-task, human-sounding text -- exactly what review_reply is
        supposed to let through). check_background_relay_worth is the
        separate, dedicated check for that; runs before translation so a
        suppressed message costs nothing further."""
        from app.plugins.voice_api import check_background_relay_worth, translate_text

        verdict = await check_background_relay_worth(text, task=task)
        if verdict is not None and not verdict["worth_relaying"]:
            log_event(
                "engine", "background_relay_suppressed_as_routine", tab_id=self.tab_id,
                text_len=len(text), reason=verdict["reason"],
            )
            return

        # Bug fix (2026-10-14), confirmed live: translate_text_failed
        # (reason=request_failed, a plain transient infra hiccup) hit at the
        # exact moment of a real relay, and this is the ONLY translation
        # opportunity a relayed background message ever gets (the
        # originating headless session never translates its own output --
        # see this method's own docstring above) -- a single failed attempt
        # here means an untranslated, wrong-language message reaching the
        # user with no second chance. One retry before falling back.
        translated = None
        for attempt in range(2):
            try:
                translated = await translate_text(
                    text, current_language_name(self.tab_id), gender=get_persona_gender(self.workspace_dir),
                    addressee_gender=get_owner_profile(self.workspace_dir).gender, context=task,
                )
            except Exception as exc:
                log_event("engine", "deliver_relayed_reply_translate_failed", tab_id=self.tab_id, error=str(exc), attempt=attempt)
                translated = None
            if translated:
                break
        final_text = translated or text
        log_event("engine", "background_reply_relayed", tab_id=self.tab_id, text_len=len(final_text))
        self.last_visible_output_at = time.monotonic()
        wire = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": final_text}], "model": None, "stop_reason": None},
            "session_id": None, "parent_tool_use_id": None,
        }
        await self.send({"type": "sdk_message", "message": wire, "isVoice": is_voice})
    def _push_message(self, text: str, attachments: list[Any], is_voice: bool) -> None:
        sent_line = f"[Sent: {_format_timestamp_for_model(datetime.now(timezone.utc).astimezone())}"
        if is_voice:
            sent_line += ", via voice input -- may contain transcription errors"
        sent_line += "]"
        content: list[dict[str, Any]] = [{"type": "text", "text": sent_line}]
        if self._stop_note_in_next_push:
            self._stop_note_in_next_push = False
            content.append({"type": "text", "text": _SYNTHETIC_TURN_MARKER + STOP_NOTE_TEXT})
        for attachment in attachments:
            content.extend(_attachment_to_blocks(attachment))
        if text:
            content.append({"type": "text", "text": text})
        self.queue.append({
            "message": {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None},
            "is_voice": is_voice,
        })
        self._queue_event.set()
    def _schedule_recent_24h_dialogue_refresh(self) -> None:
        """Rebuilds this tab's 24h-dialogue file in a WORKER THREAD, never on
        the event loop (see _write_recent_24h_dialogue_file's docstring for
        the outage that made that necessary). Single-flight: at most one
        refresh runs per tab at a time -- a request that arrives while one
        is in flight just marks it dirty so exactly one more pass runs after
        it, instead of N submits queuing N parallel transcript readers.
        Fire-and-forget by design: the file's PATH is stable and already in
        the system prompt, so nothing waits on this; a failure is logged and
        leaves the previous (stale but valid) file in place. With no running
        event loop (a plain script/test calling into ChatSession
        synchronously) it just runs inline."""
        if self._recent_24h_refresh_task is not None and not self._recent_24h_refresh_task.done():
            self._recent_24h_refresh_dirty = True
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                _write_recent_24h_dialogue_file(self.last_saved_session_id, self.tab_id, self.workspace_dir)
            except Exception as exc:
                log_event("engine", "recent_24h_dialogue_file_refresh_failed", tab_id=self.tab_id, error=str(exc))
            return
        self._recent_24h_refresh_task = loop.create_task(self._refresh_recent_24h_dialogue_async())
    async def _refresh_recent_24h_dialogue_async(self) -> None:
        """Runs the rebuild in a SEPARATE PROCESS (see dialogue_refresh_
        worker.py for the outage that made a thread not enough), at most one
        such process at a time across ALL tabs, at below-normal priority, and
        skipped outright when the file was rebuilt within the last
        RECENT_24H_REFRESH_MIN_AGE_S -- a restart storm otherwise re-parses
        the same hundreds of MB once per restart per tab. The file's PATH is
        stable and already in the system prompt, so a skipped/failed refresh
        just leaves the previous valid file in place."""
        while True:
            self._recent_24h_refresh_dirty = False
            started = time.monotonic()
            out_path = _recent_24h_dialogue_path(self.workspace_dir, self.tab_id)
            try:
                age_s = time.time() - out_path.stat().st_mtime
            except OSError:
                age_s = None
            if age_s is not None and age_s < RECENT_24H_REFRESH_MIN_AGE_S:
                log_event("engine", "recent_24h_dialogue_file_fresh_skip", tab_id=self.tab_id, age_s=round(age_s))
                return
            try:
                async with _RECENT_24H_REFRESH_SLOT:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, str(Path(__file__).resolve().parent.parent / "dialogue_refresh_worker.py"),
                        self.last_saved_session_id or "-", self.tab_id, self.workspace_dir,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0),
                    )
                    try:
                        code = await asyncio.wait_for(proc.wait(), timeout=RECENT_24H_REFRESH_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        raise RuntimeError(f"dialogue refresh worker exceeded {RECENT_24H_REFRESH_TIMEOUT_S}s and was killed")
                if code != 0:
                    raise RuntimeError(f"dialogue refresh worker exited with code {code}")
                log_event("engine", "recent_24h_dialogue_file_refreshed", tab_id=self.tab_id, ms=round((time.monotonic() - started) * 1000))
            except Exception as exc:
                log_event("engine", "recent_24h_dialogue_file_refresh_failed", tab_id=self.tab_id, error=str(exc))
            if not self._recent_24h_refresh_dirty:
                return

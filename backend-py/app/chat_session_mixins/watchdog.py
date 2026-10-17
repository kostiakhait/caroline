from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class WatchdogMixin:
    async def _watchdog_loop(self) -> None:
        try:
            while not self.ended:
                await asyncio.sleep(WATCHDOG_INTERVAL_MS / 1000)
                if self.ended:
                    return
                # Bug fix (2026-09-10): confirmed live -- one of these four
                # checks raising ANYTHING other than CancelledError used to
                # end this whole loop silently (only CancelledError was
                # caught below), permanently disabling hang-detection,
                # progress narration, the silent-user-wait nudge, AND the
                # idle-task-drift check for this tab for the rest of the
                # process's lifetime -- no log, no restart, unnoticed for
                # 20+ minutes until the downstream symptoms (no narrator
                # comments, a lamp that never blinks) got reported. A
                # single bad tick must never cost this tab everything these
                # four checks are responsible for -- log it and keep
                # ticking. supervise() (start(), task_supervisor.py) is the
                # outer safety net if this task ever dies anyway. (The old
                # fifth check here, idle-task-drift, is gone -- see
                # _fire_post_turn_completion_check()'s own doc comment for
                # why it's now event-driven, off the turn_pending setter,
                # not a periodic tick.)
                try:
                    await self._check_hang()
                    self._check_user_wait_nudge()
                    await self._check_progress_narration()
                    self._check_forced_compaction()
                    self._check_deferred_engine_switch()
                    self._check_funds_exhaustion_status()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 -- must log, never let this tick die silently
                    log_event("engine", "watchdog_tick_failed", tab_id=self.tab_id, error=str(exc), error_type=type(exc).__name__)
        except asyncio.CancelledError:
            pass
    def _check_user_wait_nudge(self) -> None:
        """Separate from hang-detection above -- that guards against a
        DEAD transport (no init, or nothing streaming at all); this guards
        against a LIVE transport where the model itself went quiet without
        ever replying to a real user message (finished a turn with no
        text, lost track mid-task, etc.). Deliberately does nothing for a
        fully hung/never-initialized query(): the nudge this queues via
        submit() sits behind the same stuck input stream as the original
        message until hang-detection's own, separate recovery runs."""
        if self.last_real_user_turn_at is None or self.real_user_turn_answered or self.silence_nudge_sent_for_turn:
            return
        if self._post_turn_checks_halted:
            return
        elapsed = time.monotonic() - self.last_real_user_turn_at
        if elapsed < SILENT_USER_WAIT_NUDGE_MS / 1000:
            return
        log_event("engine", "silent_user_wait_nudge", tab_id=self.tab_id, elapsed_s=round(elapsed, 1))
        self.silence_nudge_sent_for_turn = True
        # Bug fix (2026-09-10): confirmed live -- unlike CONTINUE_OR_SILENT_NUDGE_TEMPLATE
        # (used by the other internal nudges), this text never told the model which
        # language to answer in at all, so a reply to it could land in the wrong
        # language even once current_language_name() itself is correct.
        self.submit(
            "[Internal: it's been over 90 seconds since the user's message and nothing has reached them yet. If "
            "you're already working on something (a tool call, research, a multi-step task), just continue -- "
            "don't restart from scratch. If you actually finished and simply didn't reply, or lost track, answer "
            f"them now, directly, in {current_language_name(self.tab_id)}. Don't mention this note itself.]",
            [], False, False,
        )
    def _track_background_tasks(self, message: Any) -> None:
        """Feeds agent_registry from the SDK's typed task lifecycle events (see that module's docstring for
        why). A background task can end with a TaskNotificationMessage OR only a TaskUpdatedMessage whose
        status is terminal (e.g. a stopped task) -- either one clears it.

        Bug fix (2026-10-15), confirmed live: a real sub-agent ("Periodic 8-mailbox sweep") finished with a
        genuine TaskNotificationMessage.summary carrying its actual findings, but nothing here ever read
        that field -- the launching session had already wrapped up its OWN turn with "NOTHING NEW" before
        the sub-agent even finished, and its real report was lost for good. TaskNotificationMessage always
        carries `summary` (can be empty); a terminal TaskUpdatedMessage does not carry one at all, so only
        the TaskNotificationMessage branch can ever have something to report. On a non-empty summary, the
        session that LAUNCHED the sub-agent (self -- a real tab or its own pseudo-session, see
        app/pseudo_tab.py; this runs identically for either, no self.headless branching needed) is
        reactivated with the sub-agent's own report via self.inject_proactive -- the same uniform mechanism
        the user asked for: whichever session launched a sub-agent is the one that gets to react to it."""
        try:
            if isinstance(message, TaskStartedMessage):
                agent_registry.register(self.workspace_dir, self.tab_id, message.task_id, message.description, message.task_type, message.tool_use_id)
            elif isinstance(message, TaskNotificationMessage):
                running = agent_registry.running(self.workspace_dir, self.tab_id)
                description = (running.get(message.task_id) or {}).get("description")  # read BEFORE finish() pops it
                agent_registry.finish(self.workspace_dir, self.tab_id, message.task_id, message.status)
                summary = (message.summary or "").strip()
                if summary:
                    if pseudo_tab.is_pseudo_tab_id(self.tab_id):
                        real_tab_id = pseudo_tab.real_tab_id_from_pseudo(self.tab_id)
                        pseudo_tab.set_pseudo_turn_task(real_tab_id, description)
                        # Per explicit instruction (2026-10-15): stays on the SAME
                        # engine as its real tab, even if that tab switched mode
                        # since this pseudo-session was created/last ran.
                        pseudo_tab.sync_pseudo_tab_engine(self.workspace_dir, real_tab_id)
                    self.inject_proactive(
                        f"[Your sub-agent (agentId {message.task_id}"
                        f"{f', {description}' if description else ''}) just finished (status: {message.status}). "
                        f"Its report:\n\n{summary}\n\nYou are being reactivated specifically because this is your "
                        "own sub-agent's result -- decide what, if anything, to do with it now. If nothing further "
                        "is needed, reply exactly [[NO_UPDATE]].]",
                        suppress_narration=True,
                    )
            elif isinstance(message, TaskUpdatedMessage) and (message.status or (message.patch or {}).get("status")) in TERMINAL_TASK_STATUSES:
                agent_registry.finish(self.workspace_dir, self.tab_id, message.task_id, message.status or (message.patch or {}).get("status"))
        except Exception as exc:
            log_event("engine", "track_background_tasks_failed", tab_id=self.tab_id, error=str(exc))
    def _tell_model_agents_were_lost(self, lost: list[dict[str, Any]]) -> None:
        listing = "; ".join(f"agentId {e['task_id']}: {e.get('description') or '?'}" for e in lost)
        log_event("engine", "agents_lost_note_injected", tab_id=self.tab_id, count=len(lost))
        self.inject_proactive(
            "[The app restarted while these agents/background tasks you had launched were still running, and a "
            f"restart kills them -- they are GONE and will never report back: {listing}. If the work is still "
            "needed, launch it again (check first what they may already have produced, e.g. files on disk). Never "
            "mention the restart to the user. If nothing here needs doing, reply exactly [[NO_UPDATE]].]"
        )
        agent_registry.clear_lost(self.workspace_dir, self.tab_id)
    def _track_tool_outcomes(self, message: Any) -> None:
        """Ground-truth tool-outcome tracking (see ToolOutcome's own
        docstring and the field comments in __init__): every ToolUseBlock is
        remembered here by call id until its matching ToolResultBlock (on
        the following UserMessage) arrives, at which point it's recorded
        into turn_tool_outcomes keyed by NAME -- so a later successful retry
        of the same tool overwrites an earlier failure instead of the two
        coexisting. Called once per raw SDK message at the top of the
        per-message loop, off the exact same objects both ClaudeEngine and
        CodexEngine already produce there, so this works identically for
        either engine with zero engine-specific code. Split out as its own
        method (rather than left inline in the loop) so it can be exercised
        directly against fake SDK messages without a live engine."""
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    self._pending_tool_calls[block.id] = block.name
            return
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    tool_name = self._pending_tool_calls.pop(block.tool_use_id, None) or "unknown_tool"
                    content = block.content
                    if isinstance(content, list):
                        # SDK's own shape here is raw dicts (e.g.
                        # {"type": "text", "text": ...}), NOT TextBlock
                        # objects -- see ToolResultBlock's own type in
                        # claude_agent_sdk.types.
                        preview = " ".join(
                            item.get("text", "") for item in content
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                    elif isinstance(content, str):
                        preview = content
                    else:
                        preview = ""
                    preview = preview.strip()[:300]
                    self.turn_tool_outcomes[tool_name] = ToolOutcome(
                        name=tool_name, is_error=bool(block.is_error), result_preview=preview,
                    )
    def _turn_outcomes_summary(self) -> str:
        """Ground-truth text for the current real-user episode's tool calls
        (see turn_tool_outcomes' own __init__ comment) -- literally the
        name/is_error/content already on the wire, never inferred or
        guessed. Empty string when there's nothing to report (no tool calls
        yet, or every one of them succeeded), so callers can just check
        truthiness before appending it to a nudge. Deliberately reports
        EVERY outcome, not just errors -- a reader reconciling "did I
        actually finish" needs the full picture, not just the bad news."""
        if not self.turn_tool_outcomes:
            return ""
        lines = []
        for outcome in self.turn_tool_outcomes.values():
            status = f"ERROR ({outcome.result_preview})" if outcome.is_error else "ok"
            lines.append(f"{outcome.name} -> {status}")
        return "Ground truth for tool calls this task actually made so far (not your own account of it): " + "; ".join(lines) + "."
    def _turn_has_unresolved_tool_error(self) -> bool:
        """True when the ground-truth record (see _turn_outcomes_summary)
        shows at least one tool call still sitting at is_error=True for this
        episode -- used to refuse a blind [[NO_UPDATE]] rather than trust
        the model's own self-report over what its own tool calls actually
        returned."""
        return any(outcome.is_error for outcome in self.turn_tool_outcomes.values())
    def _fire_post_turn_completion_check(self) -> None:
        """Sanity check run right after a turn finishes -- per explicit
        correction (2026-09-13), replacing a periodic timer-based version
        (every IDLE_TASK_CHECK_INTERVAL_MS regardless of whether anything
        had happened) that the user rejected as needless expense. Called
        from the turn_pending property setter's True->False edge:
        unconditionally for a REAL user turn, or for a PROACTIVE turn that
        produced genuinely no visible content at all (see
        turn_saw_any_visible_text's own comment for that second case, added
        2026-09-13 after a real incident -- a scheduled mailbox check did
        real work but its final answer never reached the wire). Never for
        forced_compaction's own "/compact" (that bypasses turn_pending
        entirely, see _push_internal_command), and never twice for a turn
        that started on the small-model path and escalated (that hand-off
        keeps turn_pending continuously True across the switch, see
        submit_or_try_small_model's own docstring, so only the FINAL
        completion trips this edge).

        Sets _awaiting_post_turn_check_reply so THIS check's own reply
        (submitted via inject_proactive, always is_real_user=False) can't
        chain into firing the proactive-empty branch above again ON ITSELF
        if that reply comes back genuinely empty -- never an infinite tight
        loop of a check chasing its own silence.

        Bug fix (2026-09-18), per a real live incident: this flag used to
        ALSO suppress a re-check when the reply was NOT empty -- confirmed
        live, a check's reply turned into a whole further round of real
        work (tool calls, real visible text) that then ALSO ended mid-task,
        and nothing ever checked it again, leaving the tab silently stuck
        until the user manually nudged it. A reply with real visible
        content is evidence of genuine continued work, not a final answer
        -- it gets re-armed for another check right alongside a real user
        turn (see the call site), so a genuinely multi-step task keeps
        getting checked round after round until it's either actually done
        or truly falls silent, not just once per originating turn.

        Still asks the model directly rather than trusting our own
        bookkeeping (confirmed live that bookkeeping alone can be wrong --
        see hang_interrupt_result_pending's own comment) -- just event-
        driven now instead of polling on a clock."""
        if self.ended:
            return
        if self._post_turn_checks_halted:
            log_event("engine", "post_turn_completion_check_halted_by_stop", tab_id=self.tab_id)
            return
        if self._post_turn_check_rounds >= POST_TURN_CHECK_MAX_ROUNDS:
            log_event("engine", "post_turn_completion_check_round_cap", tab_id=self.tab_id, rounds=self._post_turn_check_rounds)
            return
        self._post_turn_check_rounds += 1
        lang = current_language_name(self.tab_id)
        # Ground-truth cross-check (2026-09-22, see ToolOutcome's own
        # docstring): grounds this nudge in what the last turn's own tool
        # calls actually returned, instead of leaving it pure introspection
        # -- appended, never replacing CONTINUE_OR_SILENT_NUDGE_TEMPLATE
        # itself, so every existing call site of that template elsewhere
        # keeps behaving exactly as before.
        outcomes_summary = self._turn_outcomes_summary()
        nudge = CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=lang)
        if outcomes_summary:
            nudge = f"{nudge}\n\n{outcomes_summary}"
        log_event("engine", "post_turn_completion_check", tab_id=self.tab_id, has_tool_outcomes=bool(outcomes_summary))
        self._awaiting_post_turn_check_reply = True
        self.inject_proactive(nudge)
    async def _check_hang(self) -> None:
        if load_chat_mode(self.workspace_dir, self.tab_id) == "sw":
            # sw mode (2026-10-03 bug fix, confirmed live): this whole check is built
            # around the real SDK/CLI process (self.client, self._process_activity_
            # monitor, self.last_activity -- all only ever populated by _run_loop's
            # real-session branch, which an sw-mode tab now skips entirely -- see that
            # method's own 2026-10-03 comment). None of that state exists here, so
            # `elapsed` below grows unboundedly against a last_activity nothing ever
            # refreshes -- confirmed live: hang_count climbed past 120 and
            # hang_repeat_force_close fired every single tick on a tab that was
            # actively working the whole time (small_model_engine.py has its own,
            # completely separate stall detection for this turn -- _TurnStalled/
            # STALL_TIMEOUT_S). Skip entirely; nothing here applies to this mode.
            return
        # Bug fix (2026-09-15), per explicit instruction: "Это должно быть
        # 90-секунд отсчитываемых, когда ничего не происходит: процессы не
        # потребляют процессор и не меняется загрузка памяти. Это не
        # таймаут выполнения, а таймаут от зависания." -- the real
        # liveness signal is whether the underlying claude.exe OS process
        # is doing anything (CPU/memory), not "did an SDK message arrive"
        # -- those aren't the same thing. Confirmed live via a direct
        # isolated test: a real /compact against a realistically-sized
        # session took 109.5s end to end with ZERO intermediate SDK
        # messages, while the process was genuinely working the whole
        # time -- the old signal could not tell that apart from an
        # actually frozen process. See process_activity.py's own module
        # docstring. Falls back to the old last-SDK-message clock only if
        # the monitor itself isn't usable (no pid captured yet, or psutil
        # couldn't read the process for some reason) -- "can't tell" must
        # never silently mean "assume hung", so the fallback also restores
        # the tool_in_flight extended leash from the previous version of
        # this fix, as a second safety net for exactly that degraded case.
        monitor = self._process_activity_monitor
        if monitor is not None and monitor.available:
            monitor.sample()
            elapsed = monitor.seconds_since_last_activity()
            signal_source = "process_activity"
            effective_timeout_s = (HANG_TIMEOUT_MS if self.has_seen_init else STARTUP_TIMEOUT_MS) / 1000
            tool_in_flight = False
        else:
            elapsed = time.monotonic() - self.last_activity
            signal_source = "last_sdk_message_fallback"
            tool_in_flight = (
                self.last_tool_use_started_at is not None
                and time.monotonic() - self.last_tool_use_started_at <= elapsed + 1
            )
            effective_timeout_s = (
                HANG_TIMEOUT_WITH_TOOL_IN_FLIGHT_MS if tool_in_flight
                else (HANG_TIMEOUT_MS if self.has_seen_init else STARTUP_TIMEOUT_MS)
            ) / 1000
        # Bug fix (2026-09-16), per explicit instruction: hard backstop on
        # top of the activity signal -- see HANG_ABSOLUTE_CEILING_MS's own
        # comment for the incident (a real turn stuck 15.5 minutes,
        # hang_count never left 0 because the process kept showing just
        # enough activity to look "alive" every tick). Checked against
        # turn_pending_since (when THIS turn actually started), not the
        # activity clock -- entirely independent axis.
        turn_elapsed_s = (time.monotonic() - self.turn_pending_since) if self.turn_pending_since is not None else 0.0
        # Bug fix (2026-10-16), confirmed live, a real incident: turn_elapsed_s above is
        # 0.0 whenever no turn is pending -- including a fresh session still stuck trying
        # to reach has_seen_init (tab 1: stuck 30+ minutes, hang_count never left 0,
        # turn_pending false the entire time). HANG_ABSOLUTE_CEILING_MS's own backstop
        # never applied to that case at all. Same reasoning, independent clock: how long
        # has THIS connection attempt (since connecting_since was stamped, see _run_loop)
        # been trying to reach init, regardless of what the activity monitor says.
        connecting_elapsed_s = (
            time.monotonic() - self.connecting_since
            if (self.connecting_since is not None and not self.has_seen_init) else 0.0
        )
        past_absolute_ceiling = (
            turn_elapsed_s >= HANG_ABSOLUTE_CEILING_MS / 1000
            or connecting_elapsed_s >= HANG_ABSOLUTE_CEILING_MS / 1000
        )
        log_event(
            "engine", "check_hang_tick", tab_id=self.tab_id, turn_pending=self.turn_pending,
            last_activity_s=round(elapsed, 1), hang_count=self.hang_count,
            hang_interrupted_at=self.hang_interrupted_at, has_seen_init=self.has_seen_init,
            effective_timeout_s=effective_timeout_s, signal_source=signal_source,
            tool_in_flight=tool_in_flight, tool_in_flight_name=self.last_tool_use_name if tool_in_flight else None,
            turn_elapsed_s=round(turn_elapsed_s, 1), connecting_elapsed_s=round(connecting_elapsed_s, 1),
            past_absolute_ceiling=past_absolute_ceiling,
            # Bug fix (2026-09-16): raw numbers behind the activity
            # boolean, not just the final verdict -- see ProcessActivity
            # Monitor's own comment for the incident this closes (no way
            # to tell from the log alone WHY a process kept looking
            # "active").
            monitor_cpu_percent=monitor.last_cpu_percent if monitor is not None else None,
            monitor_rss_delta=monitor.last_rss_delta if monitor is not None else None,
            monitor_io_delta=monitor.last_io_delta if monitor is not None else None,
        )
        if not self.turn_pending and self.has_seen_init:
            self.hang_interrupted_at = None
            return

        # --- Phase 2: already soft-interrupted, waiting on escalation ------
        # Bug fix (2026-09-15), confirmed live (tab 4, 2026-09-15: real
        # stream_ended_unexpectedly elapsed_ms up to 351453 -- 3-4x the
        # intended ~110s = 90s detect + 20s grace): this branch used to be
        # reachable ONLY when `elapsed` (the SAME clock phase 1 below
        # uses) was STILL past effective_timeout_s -- so the CLI's own
        # courtesy "aborted-turn" ResultMessage (which always arrives
        # moments after a soft interrupt, and always used to reset
        # last_activity) silently swallowed the whole escalation: elapsed
        # dropped back near zero, this function returned early every tick
        # from the check below, and the REAL 20s grace period never
        # actually got examined until ANOTHER full 90s of silence had
        # passed on top. Once a hang is already being escalated, only ITS
        # OWN clock (hang_interrupted_at) should gate it -- checked
        # unconditionally here, completely independent of whatever the
        # liveness signal above says in the meantime.
        if self.hang_interrupted_at is not None:
            if time.monotonic() - self.hang_interrupted_at < HANG_ESCALATION_GRACE_MS / 1000:
                return
            log_event("engine", "hang_escalation_force_close", tab_id=self.tab_id)
            self.hang_interrupted_at = None
            try:
                if self.client:
                    await self.client.disconnect()
            except Exception as exc:
                log_event("engine", "hang_escalation_close_failed", tab_id=self.tab_id, error=str(exc))
            return

        # --- Phase 1: not yet interrupted -- is THIS tick a fresh hang? ----
        # past_absolute_ceiling (computed above) can force this even while
        # the activity signal alone says "still alive" -- see
        # HANG_ABSOLUTE_CEILING_MS's own comment.
        if elapsed < effective_timeout_s and not past_absolute_ceiling:
            return
        if past_absolute_ceiling and elapsed < effective_timeout_s:
            log_event(
                "engine", "hang_absolute_ceiling_override", tab_id=self.tab_id,
                turn_elapsed_s=round(turn_elapsed_s, 1), signal_source=signal_source,
            )

        self.hang_count += 1
        # Bug fix (2026-09-10): capture what was actually running, before
        # interrupting it, so _handle_failure's replay nudge can tell the
        # model specifically what got force-terminated (per explicit
        # instruction) rather than a generic note -- lets it try something
        # else instead of blindly repeating the same slow/stuck call. Only
        # meaningful in the last_sdk_message_fallback path (tool_in_flight
        # is always False when the process-activity monitor is the one
        # driving this, since real work reaching the full 90s of genuine
        # process inactivity would itself now be a real hang either way).
        if tool_in_flight:
            self.hang_interrupted_tool_name = self.last_tool_use_name
            self.hang_interrupted_tool_elapsed_s = time.monotonic() - self.last_tool_use_started_at
        else:
            self.hang_interrupted_tool_name = None
            self.hang_interrupted_tool_elapsed_s = None
        if self.hang_count >= 2:
            log_event("engine", "hang_repeat_force_close", tab_id=self.tab_id, hang_count=self.hang_count)
            self.hang_interrupt_result_pending = True
            try:
                if self.client:
                    await self.client.disconnect()
            except Exception as exc:
                log_event("engine", "hang_force_close_failed", tab_id=self.tab_id, error=str(exc))
            return
        log_event(
            "engine", "hang_detected_soft_interrupt", tab_id=self.tab_id,
            tool_name=self.hang_interrupted_tool_name, tool_elapsed_s=self.hang_interrupted_tool_elapsed_s,
        )
        self.hang_interrupted_at = time.monotonic()
        # Bug fix (2026-09-10): confirmed live -- interrupt() is a soft
        # ask, not a hard kill; the CLI still sends a final ResultMessage
        # for the turn it just aborted. Without this flag, that
        # ResultMessage was treated exactly like a real completion --
        # wiping turn_pending/pending_user_text AND resetting silent_turn=
        # True -- so even when _handle_failure's replay genuinely
        # succeeded a few seconds later, its real answer was silently
        # swallowed (silent_turn never got reset back to False, since the
        # replay goes through _push_message directly, not submit()). See
        # the ResultMessage handler below for the other half of this fix.
        self.hang_interrupt_result_pending = True
        try:
            if self.client:
                await self.client.interrupt()
            else:
                self.hang_interrupt_result_pending = False
        except Exception as exc:
            log_event("engine", "hang_soft_interrupt_failed", tab_id=self.tab_id, error=str(exc))
            # Bug fix (2026-09-26): the flag above is set BEFORE the await on purpose (the aborted turn's
            # courtesy ResultMessage can arrive before interrupt() itself returns), but when the interrupt
            # never reached the CLI (confirmed live: "Not connected. Call connect() first." while the
            # session was mid-restart) no courtesy result is coming -- leaving the flag set would make the
            # NEXT real answer look like one and get silently swallowed, the exact failure the comment
            # above describes. Undo it.
            self.hang_interrupt_result_pending = False
        # Same reasoning as stop()'s own fix -- a hang is plausibly caused
        # by exactly a detached background operation that never completes/
        # never gets polled again, so cancel this tab's in-flight
        # operations here too, not just on an explicit user Stop.
        cancelled = REGISTRY.cancel_for_tab(self.tab_id)
        if cancelled:
            log_event("engine", "hang_soft_interrupt_cancelled_operations", tab_id=self.tab_id, count=cancelled)
    async def _handle_failure(self, exc: BaseException, extra_note: str | None = None) -> None:
        # See _format_exception_chain's own docstring for the incident this
        # fixes -- error_chain carries whatever REAL underlying cause a
        # wrapped SDK exception (CLINotFoundError et al.) would otherwise
        # hide; error is kept too, unchanged, for anything already
        # filtering/searching logs on that exact field.
        log_event(
            "engine", "handle_failure_entered", tab_id=self.tab_id, hang_count=self.hang_count,
            turn_pending=self.turn_pending, error=str(exc), error_chain=_format_exception_chain(exc),
        )
        # Bug fix (2026-09-28), confirmed live from a real incident: a crash
        # mid-compaction (ClosedResourceError right after the compaction
        # round-trip's own AssistantMessage) left forced_compaction_result_
        # pending stuck True forever, since nothing else on this path ever
        # clears it -- switch_engine_if_needed() already had this exact fix
        # for its own trigger (see its own comment: "leaving forced_
        # compaction_result_pending=True stuck ... silently hid every
        # subsequent reply from the client forever"), but only for an
        # engine switch specifically. Generalized here: ANY stream teardown
        # of ANY cause invalidates whatever fake-result bookkeeping belonged
        # to that now-dead stream -- the replay/reconnect that follows is a
        # real turn (or a real fresh compaction attempt) and must not be
        # permanently muted by a flag left over from the one that crashed.
        if self.forced_compaction_result_pending:
            self.forced_compaction_result_pending = False
            self._drain_compaction_queue()
        # Bug fix (2026-09-26), per explicit instruction ("Таймер надо сбрасывать каждый
        # раз. Никогда не сдаваться" -- reset the ceiling's clock on every reconnect,
        # never give up outright): turn_pending_since (see its own property-setter
        # comment) only used to reset on a fresh USER turn, never across a reconnect
        # WITHIN the same still-pending turn -- so once HANG_ABSOLUTE_CEILING_MS tripped
        # once, turn_elapsed_s could only keep growing from then on, past_absolute_ceiling
        # stayed permanently true for the rest of that turn, and _check_hang force-closed
        # every subsequent reconnect again within seconds of it starting, before it ever
        # got a real chance -- confirmed live, 2026-09-26: two tabs stuck in exactly this
        # loop for 34+ minutes straight, hang_count climbing every ~5s, neither one able
        # to reconnect at all. Resetting here -- right where every restart, of every
        # cause, actually begins (see this method's own docstring note above) -- gives
        # each fresh attempt the FULL ceiling window to prove itself stuck or not, instead
        # of inheriting a clock that was already expired before it even started. This does
        # NOT weaken the ceiling as a per-attempt circuit breaker (a connection that is
        # ITSELF stuck for a full effective_timeout_s still gets force-closed, same as
        # before) -- it only stops a series of otherwise-healthy reconnects from being
        # punished for a stale clock from an earlier, unrelated stall.
        if self.turn_pending:
            self.turn_pending_since = time.monotonic()
        # See restart_engine_switch's own __init__ comment. Consumed once,
        # right here, since switch_engine_if_needed()'s force_restart() ends
        # the stream the exact same way any other disconnect does -- this is
        # the one place that stream-end always lands, regardless of cause.
        engine_switch = self.restart_engine_switch
        self.restart_engine_switch = None
        # Any pending per-server reconnect retries belong to the client
        # instance that's being torn down -- _schedule_mcp_reconnect's own
        # self.client-identity check would catch this anyway, but clearing
        # here avoids leaking timers.
        self._clear_mcp_reconnect_timers()
        now = time.monotonic()
        self.restart_timestamps = [t for t in self.restart_timestamps if now - t < RESTART_WINDOW_MS / 1000]
        self.restart_timestamps.append(now)
        log_event("engine", "restart_budget", tab_id=self.tab_id, count=len(self.restart_timestamps), max=MAX_RESTARTS_PER_WINDOW)
        if len(self.restart_timestamps) > MAX_RESTARTS_PER_WINDOW:
            log_event("engine", "restart_budget_exceeded", tab_id=self.tab_id, count=len(self.restart_timestamps))
            self._set_conn_state(
                "restart_backoff",
                f"Trouble reconnecting (failed {len(self.restart_timestamps)} times in {RESTART_WINDOW_MS // 60_000}min) "
                f"-- retrying in {RESTART_BACKOFF_MS // 1000}s",
            )
            await asyncio.sleep(RESTART_BACKOFF_MS / 1000)
        self._set_conn_state("restarting", str(exc))
        # Per explicit instruction (2026-09-10, revised 2026-09-15): a
        # genuinely slow-but-alive tool call (a recursive grep/filesystem
        # scan, a slow browser evaluate, a large fetch/decode) gets a much
        # longer leash now (see _check_hang's own tool_in_flight/
        # HANG_TIMEOUT_WITH_TOOL_IN_FLIGHT_MS) rather than none at all --
        # this note only fires once that longer timeout is ALSO exceeded
        # (real work truly stuck), or when there was no tool in flight to
        # begin with (genuine dead-air silence past the plain 90s/300s).
        # Either way, tell the model exactly what got force-terminated so
        # it can try a different approach on retry instead of blindly
        # repeating the same slow/stuck call.
        if engine_switch:
            # Confirmed live (2026-09-22): the generic "internal failure,
            # infrastructure self-healing" framing below is actively
            # misleading here, and gives the newly active engine no hint
            # that it just inherited a conversation from a DIFFERENT engine
            # with none of its own memory of it -- it resumes its OWN prior
            # session (if any) via native --resume, which has a hole for
            # exactly however long the other engine was active; the only
            # bridge across that hole is the cross-engine dialogue-history
            # file (recent_dialogue_history_instruction, already in this
            # tab's system prompt). Confirmed live the model, told only
            # "you recovered from a failure, carry on", read that file but
            # then treated its narrated summary as settled history rather
            # than something to actively verify/continue -- replying
            # [[NO_UPDATE]] to a real, unresolved "continue" from the user.
            from_engine, to_engine = engine_switch
            watchdog_note = (
                f"[System note: this tab just switched engines, from {from_engine} to {to_engine}, per the "
                "user's own explicit request -- a deliberate handoff, not a failure; don't mention any 'error' "
                "or 'recovery' about it unless the user specifically asks what happened. You do NOT have this "
                f"tab's own memory of whatever happened while {from_engine} was active -- that work only exists "
                "in the cross-engine dialogue-history file this system prompt already points you at (see the "
                "instruction about it earlier in this prompt). If you haven't already, read that file FIRST, "
                "before answering anything below it -- and don't just treat what it describes as settled "
                "history: if it shows a task that isn't actually finished, or the user's own message below is "
                "asking you to continue something, actually continue it for real, the same as if you'd been "
                "working on it yourself the whole time.]"
            )
        else:
            # Per explicit instruction (2026-09-10, revised 2026-09-15): a
            # genuinely slow-but-alive tool call (a recursive grep/filesystem
            # scan, a slow browser evaluate, a large fetch/decode) gets a much
            # longer leash now (see _check_hang's own tool_in_flight/
            # HANG_TIMEOUT_WITH_TOOL_IN_FLIGHT_MS) rather than none at all --
            # this note only fires once that longer timeout is ALSO exceeded
            # (real work truly stuck), or when there was no tool in flight to
            # begin with (genuine dead-air silence past the plain 90s/300s).
            # Either way, tell the model exactly what got force-terminated so
            # it can try a different approach on retry instead of blindly
            # repeating the same slow/stuck call.
            tool_note = ""
            if self.hang_interrupted_tool_name:
                tool_note = (
                    f" The tool call in progress ('{self.hang_interrupted_tool_name}') had not finished after "
                    f"{round(self.hang_interrupted_tool_elapsed_s or 0)}s and was force-terminated -- if this is still "
                    "relevant, try a different approach instead of just repeating that same call, since whatever made "
                    "it slow or stuck likely hasn't changed."
                )
            self.hang_interrupted_tool_name = None
            self.hang_interrupted_tool_elapsed_s = None
            watchdog_note = (
                f"[System note: this session just recovered from an internal failure (hangCount={self.hang_count}):"
                f"{tool_note} {exc}.{(' ' + extra_note) if extra_note else ''} This is Caroline's own infrastructure "
                "self-healing, already handled -- for your own situational awareness only. Do not mention this or "
                "sound any alarm about it to the user unless they specifically ask what happened just now.]"
            )
        if self.pending_user_text is not None:
            log_event("engine", "handle_failure_replay_pending", tab_id=self.tab_id, text_len=len(self.pending_user_text))
            self._push_message(f"{watchdog_note}\n\n{self.pending_user_text}", self.pending_attachments, False)
        else:
            # current_language_name() is synchronous/instant (2026-09-09
            # redesign) -- this pending_user_text re-check is kept regardless
            # as cheap insurance against a real message queued by some other
            # earlier await in this same handler.
            lang = current_language_name(self.tab_id)
            if self.pending_user_text is not None:
                log_event("engine", "handle_failure_real_message_arrived_before_nudge", tab_id=self.tab_id)
                self.inject_proactive(watchdog_note)
                # Bug fix (2026-09-15): same root cause and same fix as
                # main.py's resuming_unfinished_turn -- inject_proactive()
                # just above always sets pending_is_real_user=False, but
                # pending_user_text is_not_none right here means this IS a
                # real, still-unanswered user question, just recovering
                # via internal machinery rather than a live submit(). See
                # main.py's own call site for the full incident writeup.
                self.pending_is_real_user = True
            else:
                log_event("engine", "handle_failure_continue_or_silent_nudge", tab_id=self.tab_id, lang=lang)
                refresh_language_in_background(self.last_saved_session_id, self.tab_id)
                self.inject_proactive(f"{watchdog_note}\n\n{CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=lang)}")
        log_event("engine", "handle_failure_done", tab_id=self.tab_id)

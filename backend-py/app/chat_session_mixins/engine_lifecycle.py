from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header
from app import skills


class EngineLifecycleMixin:
    async def start(self) -> None:
        # Bug fix (2026-09-10): confirmed live -- _watchdog_loop's task
        # died silently (no exception logged anywhere) and was never
        # restarted, permanently disabling hang-detection/progress-
        # narration/the silent-user-wait nudge/the idle-task-drift check
        # for that tab for the rest of the process's lifetime, unnoticed
        # for 20+ minutes. supervise() (task_supervisor.py) is the
        # standing fix: log any crash in full and restart the loop, for
        # every long-running background loop in this backend, not just
        # this one -- see its own module docstring.
        self._run_loop_task = supervise("run_loop", self._run_loop, self.tab_id)
        self._watchdog_task = supervise("watchdog", self._watchdog_loop, self.tab_id)
        self._voice_narration_task = supervise("voice_narration", self._voice_narration_loop, self.tab_id)
        if load_chat_mode(self.workspace_dir, self.tab_id) == "sw":
            asyncio.create_task(self._prewarm_model_key())
    # edition:cut claude-openai engine control
    async def _default_to_sw_without_claude(self) -> None:
        """Per explicit instruction (2026-10-22): a tab whose mode was never
        chosen starts in sw, not claude, when there is no Claude access on this
        machine and SW is usable. Otherwise the run loop would spawn a Claude
        CLI that can only fail -- and a failing CLI in such a tab is exactly
        what set off the phantom nudge turns and lost history seen on the test
        box. Runs at the top of the run loop, not in start(), so opening a tab
        never waits on the account-state lookups (both are cached and warmed
        at backend startup anyway)."""
        if has_chat_mode(self.workspace_dir, self.tab_id):
            return
        try:
            mode = await resolve_mode(self.workspace_dir, self.tab_id)
            if mode.chat_source != "none":
                return
            if not await chat_mode_eligible(self.workspace_dir, self.tab_id):
                return
        except Exception as exc:  # noqa: BLE001 -- keep the old default, never block the tab
            log_event("engine", "default_to_sw_check_failed", tab_id=self.tab_id, error=str(exc))
            return
        save_chat_mode(self.workspace_dir, self.tab_id, "sw")
        log_event("engine", "chat_mode_defaulted_to_sw", tab_id=self.tab_id)
        asyncio.create_task(self._prewarm_model_key())
    # edition:end
    async def _prewarm_model_key(self) -> None:
        """Fetches the provider key now rather than on the first question (2026-10-10):
        live, the first fast-lane call after a start took 4.9s instead of ~1.8s, the
        difference being this fetch. Cached for the process; a failure here is harmless."""
        try:
            from app.small_model_engine import prewarm_model_access
            await prewarm_model_access()
        except Exception as exc:  # noqa: BLE001
            log_event("engine", "model_key_prewarm_failed", tab_id=self.tab_id, error=str(exc))
    def dispose(self) -> None:
        log_event("engine", "dispose", tab_id=self.tab_id)
        self.ended = True
        if self._watchdog_task:
            self._watchdog_task.cancel()
        if self._voice_narration_task:
            self._voice_narration_task.cancel()
        self._reset_voice_narration()
        self._clear_api_retry_timer(clear_persisted=False)
        self._clear_one_shot_followup_timer(clear_persisted=False)
        self._clear_mcp_reconnect_timers()
        # edition:cut claude-openai engine control
        if self.client:
            asyncio.create_task(self._safe_interrupt())
        # edition:end
        # An sw turn ends with the session too, as a Claude turn does above (2026-10-22):
        # it was left running -- on the test box a disposed tab's cascade went on calling
        # its model until the backend was restarted. Cancelling the task is what Stop
        # does; the watchdog then flags the attempt and its thread ends.
        sw_turn = getattr(self, "_small_model_task", None)
        if sw_turn is not None and not sw_turn.done():
            log_event("engine", "dispose_cancelled_sw_turn", tab_id=self.tab_id)
            sw_turn.cancel()
        REGISTRY.cancel_for_tab(self.tab_id)
        self._queue_event.set()
    # edition:cut claude-openai engine control
    def force_restart(self) -> None:
        """Forces this session down the same way hang-escalation's own hard-
        close does (disconnect() -> _run_loop's own exception handling ->
        a fresh session on the SAME session id) -- for a human or a script
        to trigger directly over /api/control instead of having to kill OS
        processes by hand. See main.py's "force_restart" control op."""
        log_event("engine", "force_restart_requested", tab_id=self.tab_id)
        if self.client:
            asyncio.create_task(self._safe_disconnect(self.client))
    def _check_deferred_engine_switch(self) -> None:
        """Applies a switch that had to wait for a running turn, as soon as the
        session is idle (watchdog tick) -- nothing is ever interrupted for it."""
        if self.engine_switch_pending and not self.turn_pending and not self.ended:
            self.engine_switch_pending = False
            self.switch_engine_if_needed()
    def switch_engine_if_needed(self) -> str:
        """Called after the tab's answer source changed. Claude <-> OpenAI needs a
        fresh session (they are different agent processes); a switch to SW tears
        the running agent process down and leaves none (the run loop's sw branch
        builds no session at all). Returns "unchanged", "switching" (the running
        session was torn down and the run loop rebuilds it on the new engine,
        or idles for sw) or "busy" (a turn is in flight -- the switch is saved
        and applied by the watchdog as soon as the turn ends; nothing is
        interrupted).

        Switching to SW used to leave the Claude CLI running (seen live on the
        test box, 2026-10-22): the forced-compaction watchdog then sent it
        /compact, and the compaction's fresh session id replaced the tab's own,
        so the SW turns written to the old one vanished from the history the
        next turn reads -- "remember 4711" was forgotten one turn later."""
        mode = load_chat_mode(self.workspace_dir, self.tab_id)
        if mode == "sw":
            wanted = "sw"
            if self.client is None:
                return "unchanged"
        else:
            wanted = "openai" if mode == "openai" and openai_available(self.workspace_dir) else "claude"
            if wanted == self.engine_kind or self.client is None:
                return "unchanged"
        # The mode changed: a retry armed against the OLD engine's failure (bad key, outage)
        # must not fire into the new one -- cancelled here, before the busy check, so it
        # can't slip in while the switch waits for the running turn.
        self._clear_api_retry_timer()
        self._clear_one_shot_followup_timer()
        if self.turn_pending:
            log_event("engine", "engine_switch_deferred_turn_pending", tab_id=self.tab_id, wanted=wanted)
            self.engine_switch_pending = True
            return "busy"
        log_event("engine", "engine_switch", tab_id=self.tab_id, from_engine=self.engine_kind, to_engine=wanted)
        # All of this is Claude-CLI-specific compaction bookkeeping (dehydration/
        # forced-compaction machinery, see transcript_rotate.py's module docstring
        # and _check_forced_compaction) -- it must not survive onto the other
        # engine, which owns its own compaction and has no equivalent state.
        # Confirmed live: leaving forced_compaction_result_pending=True stuck
        # after a switch silently hid every subsequent reply from the client
        # forever (see the wire-send gate a few hundred lines down) and pinned
        # the status lamp on "Compacting conversation..." -- a real incident,
        # not a theoretical one. Any real user message queued behind that
        # compaction is replayed via the normal drain path, not dropped.
        if self.forced_compaction_result_pending:
            self.forced_compaction_result_pending = False
            self._drain_compaction_queue()
        self.needs_post_limit_compaction_check = False
        self.forced_compaction_blocked_until = 0.0
        # See restart_engine_switch's own __init__ comment -- consumed once
        # by _handle_failure, which is where force_restart()'s stream-end
        # actually lands.
        self.restart_engine_switch = (self.engine_kind, wanted)
        self.force_restart()
        return "switching"
    async def _safe_interrupt(self) -> None:
        try:
            if self.client:
                await self.client.interrupt()
        except Exception as exc:
            log_event("engine", "dispose_interrupt_failed", tab_id=self.tab_id, error=str(exc))
    async def _safe_disconnect(self, client: AgentEngine) -> None:
        """Fire-and-forget disconnect wrapper -- claude_agent_sdk's own
        subprocess_cli.py's close() has a confirmed live bug (2026-09-09):
        it doesn't guard against self._process already being None (a
        concurrent close from elsewhere, or the process having already
        exited), so it can throw a bare AttributeError
        ('NoneType' object has no attribute 'terminate'/'returncode') from
        deep inside the SDK's own internals. A disconnect() awaited
        directly is already covered by whatever try/except wraps its own
        call site (or _run_loop's own outer exception handler); this
        wrapper exists specifically for callers that fire disconnect() via
        asyncio.create_task() without awaiting it -- without a wrapper
        like this, that exception becomes an unretrieved Task exception
        (confirmed live: 'Task finished ... exception=AttributeError' with
        no owner to catch it), which is silent noise at best and, if the
        SDK's own internal state ends up inconsistent as a result, a
        plausible contributor to the process-level hangs seen the same
        night (see the migration plan's own incident writeup)."""
        try:
            await client.disconnect()
        except Exception as exc:
            log_event("engine", "safe_disconnect_failed", tab_id=self.tab_id, error=str(exc))
    def _kill_engine_process_tree(self, why: str) -> int:
        """Kills the engine process (claude.exe / codex) and every process
        under it, immediately. Returns how many processes were in the tree."""
        pid = self._cli_process_pid
        if not pid:
            try:
                pid = self.client.process_pid() if self.client else None
            except Exception:  # noqa: BLE001
                pid = None
        if not pid:
            log_event("engine", "user_stop_no_engine_pid", tab_id=self.tab_id, why=why)
            return 0
        return kill_process_tree(pid, why=f"{why} (tab {self.tab_id})")
    async def _force_stop_client(self) -> None:
        """Per explicit instruction (2026-09-15): "Кнопка стоп это
        абсолютный рубильник... Сразу по нажатии" -- no grace period, no
        "try the polite way first": fires client.interrupt() (harmless,
        occasionally lets the CLI wind down more cleanly) AND
        client.disconnect() (the real OS-process terminate()-then-kill()
        _check_hang's own hang recovery already relies on) CONCURRENTLY,
        immediately. disconnect() alone is a strict superset of what's
        needed -- interrupt() isn't awaited for or depended on, it can
        only help, never block this.

        Confirmed live (2026-09-15): even disconnect() itself can throw --
        the same claude_agent_sdk bug _safe_disconnect's own docstring
        documents (self._process already None inside the SDK's own
        close()). When it does, this was a dead end before: the turn
        stayed stuck with NEITHER interrupt() NOR disconnect() having
        actually worked, and nothing left to fall back to. Falls back to
        _force_kill_underlying_cli_process (a direct OS-level taskkill by
        PID, bypassing the SDK's own broken internals entirely) so Stop
        genuinely cannot fail to land.

        Bug fix (2026-09-15), THIRD report of this same shape of bug
        ("Стоп не останавливает всё немедленно"): the force-kill above
        used to run ONLY as a fallback, after first awaiting
        client.disconnect() for up to STOP_ESCALATION_DISCONNECT_TIMEOUT_S
        (10s) -- meaning a disconnect() that neither throws nor hangs, just
        runs its own slow internal graceful-shutdown sequence, made every
        Stop click take however long that took, contradicting "Сразу по
        нажатии" just as badly as the old soft-interrupt-then-grace-period
        design this was supposed to have replaced. _force_kill_underlying_
        cli_process is unconditionally safe to fire immediately, concurrently
        with disconnect() -- it goes straight for the real OS PID captured
        at spawn time, not through any SDK state disconnect() might also be
        touching, and taskkill against a process that's already exiting on
        its own just fails harmlessly (already caught inside that method).
        So it now fires first, unconditionally, with disconnect() awaited
        afterward purely as the SDK's own best-effort internal cleanup --
        no longer anything externally-visible depends on it finishing or
        even succeeding."""
        if self.client:
            asyncio.create_task(self._safe_interrupt())
        log_event("engine", "user_stop_force_close", tab_id=self.tab_id)
        self._force_kill_underlying_cli_process()
        try:
            if self.client:
                await asyncio.wait_for(self.client.disconnect(), timeout=STOP_ESCALATION_DISCONNECT_TIMEOUT_S)
        except Exception as exc:
            log_event("engine", "user_stop_close_failed", tab_id=self.tab_id, error=str(exc))
    def _on_cli_process_spawned(self, pid: int) -> None:
        """session_context's cli-pid-sink callback (see win_subprocess_
        patch.py) -- fires the moment a fresh claude.exe is actually
        spawned. Captures the pid (Stop's own fallback, see
        _force_kill_underlying_cli_process) AND builds this connection's
        ProcessActivityMonitor (_check_hang's real hang-detection signal,
        2026-09-15) in one place, since both are keyed off the exact same
        event and must always agree on which process they're tracking."""
        self._cli_process_pid = pid
        self._process_activity_monitor = ProcessActivityMonitor(pid)
    def _force_kill_underlying_cli_process(self) -> None:
        """Per explicit instruction (2026-09-15): mirrors BackendProcess.
        cs's own taskkill fallback for the identical class of problem (a
        graceful kill that doesn't reliably land) -- see
        _escalate_stop_if_still_pending's own comment for the incident.
        Kills the real OS PID directly, bypassing whatever broken internal
        state made client.disconnect() itself throw.

        Bug fix (2026-09-15), confirmed live -- "Стоп опять не сработал":
        the ORIGINAL version of this method reached into claude_agent_
        sdk's private client._transport._process.pid at THIS point in
        time (stop-time), and that came back None in a real incident
        (force_kill_cli_process_no_pid) at the exact same moment
        client.disconnect() was failing with the SDK's own confirmed
        'NoneType' object has no attribute 'returncode' bug -- i.e. Stop
        had no working path left at all. Use self._cli_process_pid
        instead: captured directly at process-spawn time (session_
        context's cli-pid-sink, wired in win_subprocess_patch.py), so it
        doesn't depend on the SDK's own internal bookkeeping still being
        intact at the moment something has already gone wrong. Falls back
        to the old transport introspection only if that capture somehow
        never happened (defense in depth, not the primary path anymore)."""
        pid = self._cli_process_pid
        if not pid:
            pid = self.client.process_pid() if self.client else None
        if not pid:
            log_event("engine", "force_kill_cli_process_no_pid", tab_id=self.tab_id)
            return
        log_event("engine", "force_kill_cli_process", tab_id=self.tab_id, pid=pid)
        try:
            if sys.platform == "win32":
                subprocess.Popen(
                    ["taskkill.exe", "/F", "/T", "/PID", str(pid)],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                # Linux port (2026-10-05): os.killpg, not os.kill -- the CLI
                # process is the leader of its own process group (see
                # win_subprocess_patch.py's own start_new_session fix), so this
                # reaches the same whole-tree kill taskkill's /T flag gives on
                # Windows, not just the one pid.
                import signal
                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone -- not an error for a caller whose whole point is "make sure it's dead"
        except Exception as exc:
            log_event("engine", "force_kill_cli_process_failed", tab_id=self.tab_id, error=str(exc))
    def _resolve_resume_session_id(self) -> str | None:
        stored = load_tab_session_id(self.workspace_dir, self.tab_id, self.engine_kind)
        if stored:
            return stored
        if self.engine_kind != "claude":
            return None  # no pre-existing Claude session to migrate into another engine
        if self.skip_migration_fallback_once:
            self.skip_migration_fallback_once = False
            log_event("engine", "skip_migration_fallback", tab_id=self.tab_id)
            return None
        if self.tab_id != PRIMARY_TAB_ID:
            return None
        migrated = find_most_recent_claude_session_id(self.workspace_dir)
        if migrated:
            log_event("engine", "migrated_pre_multitab_session", tab_id=self.tab_id, session_id=migrated)
            save_tab_session_id(self.workspace_dir, self.tab_id, migrated)
        return migrated
    def _reset_unrecoverable_session(self, replay_text: str | None, replay_attachments: list[Any]) -> None:
        log_event("engine", "reset_unrecoverable_session", tab_id=self.tab_id)
        archive_note = ""
        old_session_id = load_tab_session_id(self.workspace_dir, self.tab_id)
        if old_session_id:
            try:
                old_path = claude_project_dir(self.workspace_dir) / f"{old_session_id}.jsonl"
                if old_path.exists():
                    import shutil
                    directory = dehydrated_dir(self.workspace_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    archive_path = str(directory / f"{uuid_mod.uuid4()}.txt")
                    shutil.copyfile(old_path, archive_path)
                    archive_note = (
                        "[System note: this session was just reset after an unrecoverable internal error -- for "
                        "your own situational awareness only, don't alarm the user about it. The conversation "
                        f"from BEFORE this reset is fully preserved at {archive_path} -- if the user's message "
                        "below refers to something earlier that you don't have in this fresh context, Read that "
                        "file to find it before saying you don't know.]\n\n"
                    )
                    save_tab_continuity_archive(self.workspace_dir, self.tab_id, archive_path)
                    log_event("engine", "archived_unrecoverable_session", tab_id=self.tab_id, old_session_id=old_session_id, archive_path=archive_path)
                    # Bug fix (2026-10-16), confirmed live: archive_path above is the SAME
                    # oversized transcript that just overflowed this session -- a model that
                    # actually followed the note and Read it back in whole would overflow
                    # again immediately. Additional, purely additive: chunk it into an
                    # addressable index on the side (never touches archive_path itself, so
                    # main.py's expand_dehydrated_ref/the human "view archived conversation"
                    # UI keeps working against the original file unchanged). None when the
                    # transcript was small enough that reading it directly is already safe.
                    try:
                        chunk_dir = directory / f"{old_session_id}-chunks"
                        index_path = chunk_archive(Path(old_path), chunk_dir)
                        if index_path is not None:
                            archive_note += (
                                f"[System note: that archive is also split into smaller chunks, indexed at "
                                f"{index_path} -- read THAT index first (it's small), then only the specific "
                                f"chunk(s) it points to; never read {archive_path} directly, it's the full "
                                "oversized original and will overflow you again.]\n\n"
                            )
                    except Exception as exc:
                        log_event("engine", "archive_chunking_failed", tab_id=self.tab_id, error=str(exc))
            except Exception as exc:
                log_event("engine", "archive_unrecoverable_session_failed", tab_id=self.tab_id, error=str(exc))
        clear_tab_session_id(self.workspace_dir, self.tab_id)
        self.last_saved_session_id = None
        self.skip_migration_fallback_once = True
        self.unrecoverable_session_replay_text = f"{archive_note}{replay_text}" if replay_text is not None else (archive_note or None)
        self.unrecoverable_session_replay_attachments = replay_attachments
        self.restart_for_unrecoverable_session = True
        if self.client:
            asyncio.create_task(self._safe_disconnect(self.client))
    # edition:end
    def clear_tab(self) -> None:
        """Per explicit instruction (2026-09-17): a deliberate, user-
        confirmed (via the frontend's own modal -- this method trusts
        that already happened) full wipe of this tab's conversation.
        Unlike _reset_unrecoverable_session (which archives the old
        transcript and replays whatever was pending -- that reset is
        involuntary/recovery, never meant to lose anything), this is a
        genuine delete: no archive, no replay, nothing carried forward --
        see clear_tab_disk_state for the on-disk half.

        Stops/kills whatever's currently running first and unconditionally
        forces the live client to reconnect (unlike stop(), which no-ops
        when nothing's pending) -- an IDLE but still-connected client
        holds the old conversation in its own process memory regardless
        of what the on-disk file says, so leaving it alone would silently
        undo the clear the moment the user sent a new message."""
        log_event("engine", "clear_tab", tab_id=self.tab_id)
        clear_tab_disk_state(self.workspace_dir, self.tab_id)
        self.last_saved_session_id = None
        self.pending_user_text = None
        self.pending_attachments = []
        self.pending_is_real_user = False
        self.restart_pending = True
        # Bug fix (2026-09-18): the force-kill below can still surface as a
        # normal, non-exceptional trailing ResultMessage rather than a dead
        # connection -- without this flag that message was treated as a
        # genuine turn completion, which fired the post-turn-completion
        # "did you actually finish?" nudge against the now-EMPTY,
        # freshly-cleared session, producing a confusing reply that made
        # the clear look like it had silently failed. See result_is_fake.
        self.clear_tab_result_pending = True
        # Bug fix (2026-09-18), found immediately after the fix above:
        # marking the trailing ResultMessage fake means the message loop's
        # OWN normal completion path (the one that sets turn_pending=False)
        # never runs for it either -- unlike hang_interrupt_result_pending
        # (which leaves turn_pending alone deliberately, because a REPLAY
        # is expected to come through submit() and manage it itself) or
        # forced_compaction_result_pending (turn_pending is already False
        # the whole time that runs), clear_tab has explicitly declared "no
        # replay, nothing carried forward" -- there is no other path left
        # that will ever flip turn_pending back to False. Without this,
        # the tab would stay stuck showing turn_pending=True forever after
        # a clear. Set it here, synchronously, the same way pending_user_
        # text/pending_attachments above already are.
        self.turn_pending = False
        if self.small_model_active:
            if self._small_model_task is not None:
                self._small_model_task.cancel()
        # edition:cut claude-openai engine control
        elif self.client:
            asyncio.create_task(self._force_stop_client())
        # edition:end
        cancelled = REGISTRY.cancel_for_tab(self.tab_id)
        if cancelled:
            log_event("engine", "clear_tab_cancelled_operations", tab_id=self.tab_id, count=cancelled)
    # edition:cut claude-openai engine control
    async def _input_stream(self):
        while not self.ended:
            if not self.queue:
                self._queue_event.clear()
                await self._queue_event.wait()
                continue
            item = self.queue.pop(0)
            self.turn_is_voice = self.turn_is_voice or item["is_voice"]
            self._rung_advice_begin(item)    # runs alongside the memory advisor; _ladder_before_yield waits for it
            if item.get("memory_advice_for"):
                await self._glue_memory_advice(item)
            self._glue_context_recovery(item)
            self._dr_begin(item)
            await self._ladder_before_yield(item)
            self._inflight_begin()
            yield item["message"]
    async def _glue_memory_advice(self, item: dict[str, Any]) -> None:
        """The memory advisor (app/memory_advisor.py) for a real user message of the Claude/Codex engines: the
        block goes in as one more text block of the message. Never raises -- the message goes on unchanged."""
        try:
            from app.memory_advisor import memory_block

            started = time.monotonic()
            dialogue = await asyncio.to_thread(
                _read_recent_dialogue_lines, self.last_saved_session_id, self.tab_id, self.workspace_dir, 6,
            )
            dialogue_ms = round((time.monotonic() - started) * 1000)
            block = await memory_block(item["memory_advice_for"], dialogue, self.tab_id)
            log_event("engine", "memory_advisor_glue_timing", tab_id=self.tab_id, dialogue_read_ms=dialogue_ms,
                      total_ms=round((time.monotonic() - started) * 1000), block_chars=len(block))
            if block:
                item["message"]["message"]["content"].append({"type": "text", "text": _SYNTHETIC_TURN_MARKER + block})
        except Exception as exc:  # noqa: BLE001
            log_event("engine", "memory_advisor_glue_failed", tab_id=self.tab_id, error=str(exc)[:200])
    # edition:end
    async def _run_loop(self) -> None:
        set_send(lambda message: self.send(message))
        set_tab_id(self.tab_id)
        set_inject_proactive(lambda text: self.inject_proactive(text))
        register_inject_proactive(self.tab_id, lambda text: self.inject_proactive(text))
        register_permission_blocker(self.tab_id, self._on_permission_block_checked)
        register_send_action_guard(self.tab_id, self._check_action_before_send)
        register_file_transfer_guard(self.tab_id, self._check_file_transfer)
        register_action_closer(self.tab_id, self._on_action_send_completed)
        register_send_for_tab(self.tab_id, self.send)
        register_reply_watch(self.tab_id, self._on_outbound_message_sent)
        register_recent_real_user_directives(self.tab_id, self.recent_real_user_directives_text)
        # edition:cut claude cli pid
        set_cli_pid_sink(self._on_cli_process_spawned)
        # edition:end
        # Per explicit instruction (2026-10-15), after a real incident: a tab
        # that spends its WHOLE lifetime in sw mode never reaches the claude/
        # openai-specific branch below (see the sw-mode early-continue right
        # after this), which is the ONLY place self.last_saved_session_id
        # used to get populated (from _resolve_resume_session_id, deep inside
        # that branch). Confirmed live: "Tunnel Cat" (tab 5) ran several real
        # sw-mode turns across a whole evening session with a valid, already-
        # saved session id sitting on disk the entire time (tab-session-5.json)
        # -- _append_small_model_turn_to_session hit its own no-session guard
        # on every single one (small_model_turn_not_persisted_no_session), so
        # none of that conversation ever reached the CLI-resumable transcript.
        # Switching the SAME tab to claude mode afterward resumed that real,
        # valid session -- but from its own last REAL checkpoint, a day and a
        # half earlier, with zero knowledge of anything since: the model had
        # no way to know the conversation it could see on screen had ever
        # happened at all. Loaded here, once, unconditionally, before either
        # branch below runs -- cheap (one file read), and harmless to a tab
        # that goes on to run claude/openai turns normally: that branch's own
        # _resolve_resume_session_id() (which also handles the pre-multi-tab
        # migration fallback, not needed here) still re-sets it the same way
        # moments later, this is strictly an earlier, narrower first read.
        if self.last_saved_session_id is None:
            self.last_saved_session_id = load_tab_session_id(self.workspace_dir, self.tab_id, self.engine_kind)
        # edition:cut claude-openai session loop
        await self._default_to_sw_without_claude()
        while not self.ended:
            try:
                if load_chat_mode(self.workspace_dir, self.tab_id) == "sw":
                    # sw mode (2026-10-03 bug fix, confirmed live): submit()/
                    # inject_proactive() route every turn straight to the small-model
                    # cascade (_route_sw_turn) and never feed this loop's own
                    # _input_stream() queue -- so the real SDK session this loop used
                    # to build below was pure waste for an sw-mode tab (a full `claude`
                    # CLI subprocess spun up just to sit idle forever, since nothing
                    # ever sends it anything) AND a real, confirmed bug source: that
                    # idle subprocess crashing ("Fatal error in message reader") still
                    # flipped conn_state to "restarting", with no guaranteed path back
                    # to "connected" for a session nothing was actually using -- the UI
                    # status bar got stuck on a stale error indefinitely while the
                    # small-model path kept working underneath it, confusingly (the tab
                    # LOOKED dead while it wasn't). Skip building any real session at
                    # all in this mode; just report healthy and idle, re-checking
                    # whether the mode itself changed (a real reason to start one).
                    # Any client left over from before a switch to sw is already
                    # torn down (switch_engine_if_needed); drop the reference so
                    # nothing keeps treating it as this tab's live agent.
                    self.client = None
                    self._set_conn_state("connected")
                    while not self.ended and load_chat_mode(self.workspace_dir, self.tab_id) == "sw":
                        await asyncio.sleep(5.0)
                    continue
                self.hang_count = 0
                self.has_seen_init = False
                self.connecting_since = time.monotonic()
                # Bug fix (2026-09-10): confirmed live -- last_activity is only
                # ever refreshed by submit() (a real user message) or a message
                # actually arriving on the wire. Once a session starts failing
                # and replaying its pending turn via _push_message (NOT
                # submit(), see handle_failure), last_activity goes stale and
                # NEVER updates again while nothing streams in. Every fresh
                # query() this loop creates then inherits that ancient
                # timestamp -- _check_hang sees an "elapsed" of many minutes
                # (confirmed live: 3246s and climbing) against the 300s
                # startup timeout, so it judges the brand-new query "hung" on
                # its very first watchdog tick and force-closes it around 22s
                # in, long before even a legitimately slow resume (tab 1 has
                # needed 128-280s for a real one) can ever finish -- a
                # self-perpetuating trap this tab could never escape. A fresh
                # attempt deserves its own fresh clock.
                self.last_activity = time.monotonic()
                # Bug fix (2026-09-10): confirmed live -- progress narration
                # (_check_progress_narration) fired 5s after a fresh restart
                # (mid tool-use-concurrency recovery), because
                # last_visible_output_at was still whatever stale value it
                # had from BEFORE the whole restart cascade -- the 60s clock
                # needs to restart from a genuine restart too, not just a
                # real user submit() or an actual assistant reply, or it can
                # fire almost immediately with stale/pre-restart context.
                self.last_visible_output_at = time.monotonic()
                # Bug fix (2026-10-25), confirmed live (tab 2, real incident: a
                # ~15min hang/escalation/restart cycle repeating forever, no
                # narrator, no visible output, turn_pending never clearing):
                # hang_interrupt_result_pending is set True by the soft
                # interrupt (_check_hang) to swallow the CLI's own courtesy
                # ResultMessage for the turn it just aborted -- but it's only
                # ever cleared when that ResultMessage actually arrives and is
                # consumed (see the ResultMessage handler below). Escalation's
                # hard client.disconnect() (as opposed to the gentler
                # interrupt()) does not reliably deliver that courtesy result,
                # so the flag survived into this fresh session and silently
                # discarded the NEW session's own first real result as fake,
                # forever, every cycle -- the turn never actually completed,
                # it just sat idle until the next absolute-ceiling timeout 900s
                # later force-closed an already-dead connection and repeated
                # the same trap. A fresh session has nothing left to wait for
                # from the old one -- same "fresh attempt, fresh clock"
                # reasoning as last_activity/last_visible_output_at just above.
                self.hang_interrupt_result_pending = False
                self.ignore_next_result_recovery = False
                self._ladder_reset()
                self._dr_reset()
                self._inflight_reset()
                log_event("engine", "run_loop_fresh_session", tab_id=self.tab_id)

                self.engine_kind = (
                    "openai" if load_chat_mode(self.workspace_dir, self.tab_id) == "openai" and openai_available(self.workspace_dir)
                    else "claude"
                )
                mode = await resolve_mode(self.workspace_dir, self.tab_id)
                self.current_chat_source = "openai" if self.engine_kind == "openai" else mode.chat_source
                if mode.chat_source == "none" and self.engine_kind == "claude":
                    # query() is about to fail on its very first real request no
                    # matter what -- there's no chat source to even try. Checked
                    # here, before query() creation, so opening the login window
                    # is native/deterministic instead of depending on the model
                    # being able to run a tool call it has no chat source to run
                    # WITH (see sw_gate.py's own docstring).
                    gate = await require_sw_or_prompt(self.send, True)
                    log_event("engine", "no_chat_source", tab_id=self.tab_id, gate_ok=gate.ok, gate_message=gate.message)

                anthropic_env: dict[str, str] | None = None
                try:
                    anthropic_env = await build_options_env(self.workspace_dir, mode)
                except Exception as exc:
                    log_event("engine", "build_options_env_failed", tab_id=self.tab_id, error=str(exc))

                resume_session_id = self._resolve_resume_session_id()
                if resume_session_id:
                    self.last_saved_session_id = resume_session_id
                if resume_session_id and self.engine_kind == "claude":
                    # No CLI process has this transcript open right now (a
                    # fresh query() is about to be built), so it is safe to
                    # move a huge already-compacted prefix out of the file
                    # the CLI has to read on every resume.
                    await asyncio.to_thread(
                        rotate_transcript,
                        claude_project_dir(self.workspace_dir) / f"{resume_session_id}.jsonl",
                        self.workspace_dir,
                    )
                    # Bug fix (2026-10-12), root-caused from a real incident (tab 3,
                    # repeated hang_escalation_force_close every ~2 minutes, 7 cycles,
                    # zero SDK messages, ProcessActivityMonitor never available any
                    # cycle): last_activity was last set at the top of this while-loop
                    # iteration (now self.last_activity above, well before this point),
                    # and nothing refreshes it again until a real SDK message arrives
                    # post-connect. rotate_transcript's own full-file backward scan on
                    # an oversized (observed: tens of MB in this very workspace, see
                    # transcript_rotate.py's docstring -- 147/368 MB precedent) transcript
                    # with no compact_boundary yet can legitimately run for a couple of
                    # minutes; with no subprocess spawned yet there is no
                    # ProcessActivityMonitor to vouch for that time as real work, so
                    # _check_hang's only signal is the stale last_activity timestamp --
                    # exactly long enough to look hung on the fallback path, force-
                    # closing a connection attempt that was never actually stuck, right
                    # before it could reach connect() and start the real subprocess.
                    # Claiming a fresh timestamp here (this phase's own, bounded, single-
                    # threaded work just finished) is the same principle _check_hang
                    # already applies to the CLI subprocess itself -- legitimate slow
                    # work must not read as a hang.
                    self.last_activity = time.monotonic()
                    # Bug fix (2026-09-18), confirmed live: _recent_24h_
                    # dialogue_file_path used to only ever get (re)written
                    # inside submit()'s real-user branch -- meaning the
                    # VERY FIRST turn of a freshly (re)built session (the
                    # resumed-unfinished-turn injection included, which is
                    # is_real_user=False and so never reaches that branch
                    # at all) had recent_dialogue_history_instruction
                    # return "" -- no safety net whatsoever right when a
                    # restart makes Caroline most likely to have lost track
                    # of what she was doing. Confirmed live: exactly this
                    # produced a real incident where a resumed task's own
                    # details had to be re-explained by the user because
                    # nothing pointed her at where to look. Populate it
                    # here too, proactively, on every fresh session build --
                    # not conditional on a real submit() having happened
                    # yet this process lifetime.
                    #
                    # Bug fix (2026-09-20), confirmed live: that eager
                    # write used to be SYNCHRONOUS here, on the event loop,
                    # reading each tab's WHOLE transcript -- with four tabs
                    # starting at once (two of them 147/368 MB) the backend
                    # stopped answering /api/status for minutes and its
                    # watchdog killed it, repeatedly. Now only the (stable)
                    # PATH is set below, instantly, and the content is
                    # built by a single-flight worker-thread refresh
                    # reading just the last 24 hours (see
                    # _schedule_recent_24h_dialogue_refresh) -- moved OUT
                    # of this `if` too: right after an unrecoverable-
                    # session reset there is no resume id but there IS a
                    # continuity archive, exactly when this file matters.
                self._recent_24h_dialogue_file_path = str(_recent_24h_dialogue_path(self.workspace_dir, self.tab_id))
                self._schedule_recent_24h_dialogue_refresh()

                mcp_servers = build_mcp_servers()
                # Dynamic backstop (2026-09-22), zero hardcoded names or
                # keywords -- see compute_foreign_tool_overlap's own
                # docstring. What THIS turn disallows is exactly what the
                # PREVIOUS init message actually observed on the wire
                # (refreshed below once this connection's own init
                # arrives); empty on this process's very first-ever query,
                # self-healing one query later and from every query after
                # that, including across a full restart (cached to disk).
                dynamic_disallowed_tools = load_discovered_foreign_tool_overlap(self.workspace_dir)
                self._system_prompt_language = current_language_name(self.tab_id)
                system_prompt_parts = [
                    persona_system_prompt_append(get_persona(self.workspace_dir)),
                    owner_profile_system_prompt_clause(get_owner_profile(self.workspace_dir)),
                    instruction_memory_system_prompt_clause(self.workspace_dir),
                    working_memory_system_prompt_clause(load_working_memory(self.workspace_dir)),
                    memory_topics_system_prompt_clause(self.workspace_dir, self.tab_id),
                    *[fn() for fn in ALWAYS_ON_INSTRUCTIONS],
                    continuity_pointer_instruction(load_tab_continuity_archive(self.workspace_dir, self.tab_id)),
                    recent_dialogue_history_instruction(self._recent_24h_dialogue_file_path),
                    # The skill catalog (app/skills.py): the same text the SW engine gets; use_skill opens one.
                    skills.catalog_clause(self.workspace_dir),
                    running_agents_pointer_instruction(agent_registry.ensure_status_file(self.workspace_dir, self.tab_id)),
                    pseudo_tab_pointer_instruction(not self.headless),
                    language_hint_instruction(self._system_prompt_language),
                    OPENAI_TOOL_HONESTY_INSTRUCTION if self.engine_kind == "openai" else None,
                ]
                system_prompt_append = "\n\n".join(p for p in system_prompt_parts if p)

                def _stderr_handler(data: str, _resume_session_id: str | None = resume_session_id) -> None:
                    log_event("engine", "claude_stderr", tab_id=self.tab_id, resume=_resume_session_id, data=data.strip()[:500])
                    if SESSION_NOT_FOUND_PATTERN.search(data) and not self.restart_for_unrecoverable_session:
                        log_event("engine", "session_not_found_on_disk", tab_id=self.tab_id, resume=_resume_session_id)
                        self._reset_unrecoverable_session(self.pending_user_text, self.pending_attachments)

                options_kwargs: dict[str, Any] = {
                    "cwd": self.workspace_dir,
                    "permission_mode": "bypassPermissions",
                    "mcp_servers": mcp_servers,
                    # ScheduleWakeup/CronCreate/CronList/CronDelete (2026-10-19, per explicit
                    # instruction, confirmed live): the underlying Claude Agent SDK/CLI's own
                    # NATIVE self-scheduling tools -- not Caroline's own (absent from
                    # build_mcp_servers()) -- resubmit a prompt directly into whatever session
                    # called them, completely independent of submit()/inject_proactive(), and
                    # so completely bypass the pseudo-tab architecture (app/pseudo_tab.py) this
                    # whole system prompt otherwise enforces. Confirmed live: the model used
                    # ScheduleWakeup from a REAL, visible tab's own live session to self-
                    # reschedule an ongoing background check, re-waking that same live session
                    # repeatedly and eventually mixing a background status check with the user's
                    # own real question in the same turn. Caroline's own schedule_reminder
                    # (app/plugins/scheduler_plugin.py) is the correct replacement for ANY
                    # "check again later" need -- it only ever persists a schedule/note and
                    # always fires later through the tab's own dedicated pseudo-session, never
                    # executing in the caller's own session. Blocked unconditionally, for every
                    # ChatSession (real tabs and pseudo-sessions alike -- a pseudo-session's own
                    # recurring work is already driven externally by the scheduler's own poll
                    # loop, never by self-scheduling from inside its own turn either).
                    "disallowed_tools": [
                        "mcp__caroline-notes__notes_login", "ScheduleWakeup", "CronCreate", "CronList", "CronDelete",
                        *dynamic_disallowed_tools,
                    ],
                    # Only Caroline's OWN in-process MCP servers -- ignore every other MCP configuration (user/
                    # project scope in ~/.claude.json, claude.ai connectors). Confirmed live (2026-09-24): those
                    # belong to whoever's dev setup shares that file, come and go with their connection state,
                    # and agents reached for them (an external email MCP) instead of Caroline's own tools. A
                    # structural fix with no names or lists in it; applies to subagents too. Verified with a real
                    # probe that the Claude login is unaffected.
                    "extra_args": {"strict-mcp-config": None},
                    # Caroline's own definition of the default agent -- carries her rules, which a subagent does
                    # NOT inherit from --append-system-prompt. See agent_definitions.py.
                    "agents": caroline_agents(),
                    "stderr": _stderr_handler,
                    # Claude's own native auto-compaction handles context
                    # ageing now -- explicitly on, and one long-lived client
                    # across turns (no per-turn transcript rewrite/restart).
                    # settings wants a FILE PATH, not raw JSON -- see
                    # _ensure_settings_file's own docstring for the bug this
                    # fixes.
                    "settings": _ensure_settings_file(self.workspace_dir),
                    "hooks": {"PreCompact": [HookMatcher(hooks=[self._pre_compact_hook])]},
                    # Bug fix (2026-09-13), confirmed live (tab 2, "Flying
                    # Squirrel", twice -- 2026-09-11 and again 2026-09-13):
                    # the SDK's own subprocess transport frames the CLI's
                    # NDJSON stdout one line at a time and hard-fails
                    # (SDKJSONDecodeError, "JSON message exceeded maximum
                    # buffer size") past its default 1MB-per-line limit -- a
                    # single large tool result (base64 image content, here)
                    # is enough to cross it. That failure is NOT recoverable
                    # by this class's own restart/replay machinery once it
                    # happens: the oversized line is already persisted in
                    # the resumed session's own transcript, so the very next
                    # `resume` reads it right back off disk and dies again
                    # immediately -- confirmed live as a real, indefinite
                    # "query() stream ended unexpectedly" retry loop (over
                    # an hour, every ~5min, restart budget never even
                    # tripped because each cycle "succeeded" at restarting
                    # only to fail the same way seconds later). Raised well
                    # past any plausible single-message size this app
                    # produces (a handful of embedded images, at most) --
                    # None keeps inheriting the SDK's own default forever if
                    # this line is ever removed, so pin an explicit value.
                    "max_buffer_size": 50 * 1024 * 1024,
                }
                if anthropic_env:
                    options_kwargs["env"] = anthropic_env
                if resume_session_id:
                    options_kwargs["resume"] = resume_session_id
                claude_model = get_model_override(self.workspace_dir, "claude")
                if claude_model:
                    options_kwargs["model"] = claude_model
                if system_prompt_append and self.engine_kind != "openai":
                    # Bug fix (2026-09-26), per explicit decision: the composed prompt used to go on the
                    # claude.exe COMMAND LINE (--append-system-prompt <text>). Windows caps a command line at
                    # 32767 chars; with ALWAYS_ON at ~19.7K the tabs that carry a continuity pointer (2 and 4)
                    # reached 33115 -> CreateProcess failed with WinError 206, which the SDK misreports as
                    # "Claude Code not found at ...claude.exe" (the file was there the whole time) -- both tabs
                    # sat in "Trouble reconnecting" and never started (confirmed live, 2026-09-26), and tabs 1/3
                    # were 716 chars from the same fate. A FILE has no such limit, and the CLI reads the same
                    # text from it (--append-system-prompt-file), so nothing in the prompt is trimmed or
                    # changed. system_prompt stays the bare claude_code preset (NOT None: the SDK turns None
                    # into --system-prompt "", which would blank the base prompt).
                    prompt_file = _write_system_prompt_file(self.workspace_dir, self.tab_id, system_prompt_append)
                    options_kwargs["extra_args"] = {**options_kwargs["extra_args"], "append-system-prompt-file": prompt_file}
                    options_kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code"}
                query_started_at = time.monotonic()
                log_event("engine", "query_creating", tab_id=self.tab_id, resume=resume_session_id, chat_source=self.current_chat_source, engine=self.engine_kind)
                if self.engine_kind == "openai":
                    self.client = CodexEngine(build_codex_options(self.workspace_dir, system_prompt_append, mcp_servers, resume_session_id))
                else:
                    options = ClaudeAgentOptions(**options_kwargs)
                    self.client = ClaudeEngine(options)
                self._cli_process_pid = None
                self._process_activity_monitor = None
                # A fresh CLI process: every agent the previous one had running died with it, silently.
                lost_agents = agent_registry.mark_running_as_lost(self.workspace_dir, self.tab_id)
                await self.client.connect(self._input_stream())
                if lost_agents:
                    self._tell_model_agents_were_lost(lost_agents)

                async for raw_message in self.client.receive_messages():
                    self.last_activity = time.monotonic()
                    message: Any = raw_message

                    # Bug fix (2026-09-10): tracks whatever tool call is
                    # currently in flight so, if a hang fires while one is
                    # running, _check_hang/_handle_failure can tell the
                    # model specifically what got force-interrupted (per
                    # explicit instruction) instead of a generic "something
                    # failed" note. Never explicitly cleared on completion --
                    # if the tool finished, a NEW message would have arrived
                    # and reset last_activity anyway, so a hang could only
                    # ever fire while this really is the one still running.
                    if isinstance(message, AssistantMessage):
                        tool_use_block = next((b for b in message.content if isinstance(b, ToolUseBlock)), None)
                        if tool_use_block:
                            self.last_tool_use_name = normalize_call(tool_use_block.name, tool_use_block.input)[0]
                            self.last_tool_use_started_at = time.monotonic()

                    # Ground-truth tool-outcome tracking -- see
                    # _track_tool_outcomes' own docstring.
                    self._track_tool_outcomes(message)
                    await self._track_background_tasks(message)

                    # --- classifier refusal ---
                    if isinstance(message, AssistantMessage):
                        refusal_text = next(
                            (b.text for b in message.content if isinstance(b, TextBlock) and CLASSIFIER_REFUSAL_PATTERN.search(b.text)),
                            None,
                        )
                        if refusal_text:
                            category = extract_classifier_refusal_category(refusal_text)
                            if self.classifier_refusal_retry_count == 0:
                                self.classifier_refusal_retry_count += 1
                                log_event("engine", "classifier_refusal_retry", tab_id=self.tab_id, category=category)
                                self._push_message(
                                    "[System note: your previous reply was blocked by an Anthropic API "
                                    "content-classifier false positive (unrelated to the actual conversation) and "
                                    "never reached the user. Please just try answering their last message again.]",
                                    [], False,
                                )
                                continue
                            log_event("engine", "classifier_refusal_recurred", tab_id=self.tab_id, category=category)
                            # English like every text in the code: this becomes an
                            # assistant message, which _translate_wire_visible_text
                            # puts into the conversation's language before sending.
                            explanation = (
                                "I couldn't answer the previous message: Anthropic's internal safety filter was "
                                "triggered" + (f" (category \"{category}\")" if category else "") +
                                ", most likely a false positive unrelated to what we were discussing. The retry "
                                "failed too. Please rephrase the message or try again a little later."
                            )
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    block.text = explanation
                            self.classifier_refusal_retry_count = 0

                    # --- billing_error (structured field) ---
                    if isinstance(message, AssistantMessage) and message.error == "billing_error":
                        log_event("engine", "billing_error", tab_id=self.tab_id, chat_source=mode.chat_source)
                        explanation = self._handle_balance_exhausted()
                        self._set_conn_state("billing_blocked", explanation, arm_ignore_next_result=True)
                        self._schedule_api_retry("billing_error", self.turn_is_voice)
                        continue

                    # --- PROMPT_TOO_LONG / TOOL_CONCURRENCY / NOT_LOGGED_IN ---
                    if isinstance(message, AssistantMessage):
                        text_blocks = [b.text for b in message.content if isinstance(b, TextBlock)]
                        prompt_too_long = next((t for t in text_blocks if PROMPT_TOO_LONG_PATTERN.search(t)), None)
                        if prompt_too_long:
                            # Native auto-compaction should keep the context
                            # under the limit; if this still fires, the
                            # session is somehow past saving -- abandon it and
                            # start fresh (the pre-reset transcript is archived
                            # for Read, same as any unrecoverable reset).
                            log_event("engine", "prompt_too_long", tab_id=self.tab_id, text=prompt_too_long[:200])
                            self._set_conn_state("restarting", "Recovering (context overflow)...")
                            self._reset_unrecoverable_session(self.pending_user_text, self.pending_attachments)
                            continue
                        tool_concurrency = next((t for t in text_blocks if TOOL_CONCURRENCY_ERROR_PATTERN.search(t)), None)
                        if tool_concurrency:
                            log_event("engine", "tool_concurrency_error", tab_id=self.tab_id, text=tool_concurrency[:200])
                            self._set_conn_state("restarting", "Recovering (unrecoverable session)...")
                            self._reset_unrecoverable_session(self.pending_user_text, self.pending_attachments)
                            continue
                        not_logged_in = next((t for t in text_blocks if NOT_LOGGED_IN_PATTERN.search(t)), None)
                        if not_logged_in:
                            log_event("engine", "not_logged_in_suppressed", tab_id=self.tab_id)
                            # Bug fix (2026-09-12): unlike its two siblings just above
                            # (prompt_too_long, tool_concurrency), this branch used to just
                            # log and swallow the turn -- carolineStatus was left at
                            # whatever it was before (usually "ready"), so the lamp stayed
                            # green and the user got zero feedback that nothing will ever
                            # come back until they sign in. Genuinely distinct from
                            # billing_blocked/limited (a logged-in user out of quota, which
                            # DOES self-heal via _schedule_api_retry) -- this is "no chat
                            # source configured at all" (see subscription_mode.resolve_mode),
                            # which no amount of retrying fixes, so it maps to the same
                            # "error" public state as billing_blocked, not "recovering".
                            self._set_conn_state(
                                "not_logged_in",
                                "You don't have an active Claude Code login or a SquirrelWisdom account "
                                "signed in -- please sign in to one of them to continue.",
                            )
                            continue

                    # --- typed engine error: never a chat bubble ---
                    if isinstance(message, AssistantMessage) and message.error in ENGINE_ERROR_SUPPRESSED_KINDS:
                        raw_error_text = " ".join(b.text for b in message.content if isinstance(b, TextBlock))
                        log_event("engine", "engine_error_suppressed", tab_id=self.tab_id, error_kind=message.error, text=raw_error_text[:500])
                        if message.error == "authentication_failed":
                            self._set_conn_state("auth_failed", "The AI provider rejected the credentials -- check the login/API key in Settings.", arm_ignore_next_result=True)
                        else:
                            self._set_conn_state("engine_error", "The AI provider returned an error. Try again in a moment.", arm_ignore_next_result=True)
                        # The trailing ResultMessage is a REAL completion (clears turn_pending) but must
                        # not flip this status straight back to "connected" -- same flag the usage-cap
                        # branch below uses. The flat retry stays ON for every kind, auth included,
                        # per explicit instruction: credentials/tokens can be fixed at any moment (a
                        # refreshed login, a new key) and the session must pick that up by itself.
                        # What changed is only that the failure is silent in the chat, and that the
                        # retry is cancelled on an engine/mode switch (switch_engine_if_needed).
                        #
                        # Bug fix (2026-10-16), confirmed live (tab stuck ~8 hours on "The AI provider
                        # returned an error" with no retry ever firing again): suppress_next_conn_state_
                        # reset above only protects conn_state itself from being reset by this same
                        # turn's own trailing ResultMessage -- it does NOT protect the retry timer
                        # _schedule_api_retry is about to arm below. That ResultMessage still hit the
                        # "else: self._clear_api_retry_timer()" branch further down (gated on the
                        # SEPARATE ignore_next_result_recovery flag, which this call site never set),
                        # silently cancelling the just-scheduled retry in the same breath it was
                        # created -- exactly the 2026-09-22 cc_cli_limit_message bug, just never fixed
                        # here. arm_ignore_next_result=True closes it the same way.
                        self.suppress_next_conn_state_reset = True
                        self._schedule_api_retry(f"engine_error:{message.error}", self.turn_is_voice)
                        continue

                    # --- CC CLI usage-cap message ---
                    if isinstance(message, AssistantMessage):
                        text_blocks = [b.text for b in message.content if isinstance(b, TextBlock)]
                        limit_text = next((t for t in text_blocks if CC_CLI_LIMIT_PATTERN.search(t)), None)
                        if limit_text:
                            log_event("engine", "cc_cli_limit_message", tab_id=self.tab_id)
                            # The limit message is OUR forced compaction's own
                            # failure ("Error during compaction: You've hit your
                            # session limit"), not a real turn hitting the cap --
                            # maintenance, so it must not flip the whole tab
                            # limited/connected. See _note_compaction_hit_limit.
                            if self.forced_compaction_result_pending:
                                self._note_compaction_hit_limit(limit_text)
                                continue
                            # Bug fix (2026-09-11), per explicit instruction: this is
                            # NOT the same shape as a rate-limit REJECTION (no
                            # arm_ignore_next_result here, deliberately) -- a real
                            # AssistantMessage came through, meaning the turn
                            # genuinely completed; the model just concluded it by
                            # reporting a usage cap. The trailing ResultMessage is
                            # therefore a REAL completion, not a fake one -- let it
                            # clear turn_pending normally instead of pretending the
                            # turn never happened. _schedule_api_retry (which retries
                            # every 90s FOREVER, correct only for "the operation never
                            # even started" -- a true rejection, no balance) does not
                            # apply here; see _schedule_one_shot_followup_check's own
                            # doc comment for the distinction.
                            self._set_conn_state("limited", limit_text)
                            self.suppress_next_conn_state_reset = True
                            self._schedule_one_shot_followup_check("cc_cli_limit_message", self.turn_is_voice)
                            continue

                    # --- structured rate-limit event ---
                    if isinstance(message, RateLimitEvent):
                        info = message.rate_limit_info
                        info_dict = {"status": info.status, "resets_at": info.resets_at, "rate_limit_type": info.rate_limit_type}
                        self.last_rate_limit_info = info_dict
                        if info.status == "rejected":
                            self.ignore_next_result_recovery = True
                            self._handle_rate_limit_rejected("in-stream", info_dict)
                        continue

                    # --- system/status (compacting progress), only logged while
                    # WE triggered it -- see FORCED_COMPACTION_HOURLY_MS. Not a
                    # `continue` -- the wire-send suppression a few lines down
                    # (result_is_fake/forced_compaction_result_pending) already
                    # covers not showing this to the client; this is purely a
                    # side-effect log line, everything else about this message
                    # still flows through the loop normally.
                    if isinstance(message, SystemMessage) and message.subtype == "status" and self.forced_compaction_result_pending:
                        log_event(
                            "engine", "forced_compaction_status", tab_id=self.tab_id,
                            status=message.data.get("status"), compact_result=message.data.get("compact_result"),
                            compact_error=message.data.get("compact_error"),
                        )

                    # --- system/compact_boundary (2026-09-15) -- fires for
                    # EVERY compaction, ours (forced) or the CLI's own
                    # native auto-compaction alike, carrying the authoritative
                    # post-compaction token count in compact_metadata.
                    # post_tokens -- confirmed live via a direct isolated
                    # test (a real /compact against a copy of a real bloated
                    # session: pre_tokens=69422, post_tokens=10158). This is
                    # the new growth-trigger baseline (see
                    # tokens_at_last_forced_compaction's own __init__
                    # comment for why file bytes were the wrong signal).
                    if isinstance(message, SystemMessage) and message.subtype == "compact_boundary":
                        post_tokens = (message.data.get("compact_metadata") or {}).get("post_tokens")
                        log_event(
                            "engine", "compact_boundary_observed", tab_id=self.tab_id,
                            pre_tokens=(message.data.get("compact_metadata") or {}).get("pre_tokens"),
                            post_tokens=post_tokens, trigger=(message.data.get("compact_metadata") or {}).get("trigger"),
                        )
                        self._schedule_compaction_check()
                        if isinstance(post_tokens, int):
                            self.tokens_at_last_forced_compaction = post_tokens
                            # A boundary is itself the freshest possible
                            # reading of "what's in context right now" --
                            # keep the two baselines in sync so the growth
                            # branch doesn't compare a stale pre-compaction
                            # last_known_context_tokens against the brand
                            # new baseline on its very next check.
                            self.last_known_context_tokens = post_tokens
                        # Compaction can summarize away who was launched -- say so again, from ground truth.
                        still_running = agent_registry.running(self.workspace_dir, self.tab_id)
                        if still_running:
                            listing = "; ".join(f"agentId {i}: {e.get('description') or '?'}" for i, e in still_running.items())
                            self.inject_proactive(
                                "[Your context was just compacted. Agents/background tasks you launched that are STILL "
                                f"RUNNING and will report back on their own: {listing}. Do not re-launch them and do not "
                                "assume they are done; the full, current list is in the file named in your instructions. "
                                "If this changes nothing for the user, reply exactly [[NO_UPDATE]].]"
                            )

                    # --- system/api_retry ---
                    if isinstance(message, SystemMessage) and message.subtype == "api_retry":
                        self.last_api_retry_error = message.data.get("error")
                        log_event("engine", "api_retry_system_message", tab_id=self.tab_id, error=self.last_api_retry_error)
                        # Real incident (2026-09-09): an "authentication_failed" api_retry
                        # is just logged-and-waited here, with NOTHING that ever tears the
                        # connection down -- the CLI keeps retrying the exact same doomed
                        # request against the exact same (possibly stale) session/env
                        # forever. After a bounded number of consecutive auth failures on
                        # this connection, force a clean restart -- the next _run_loop
                        # iteration re-resolves the mode and rebuilds Options.env from
                        # scratch. Own choice of threshold (3), not a specified value.
                        if self.last_api_retry_error == "authentication_failed":
                            self.consecutive_auth_retry_failures += 1
                            log_event("engine", "auth_retry_failure_streak", tab_id=self.tab_id, count=self.consecutive_auth_retry_failures)
                            if self.consecutive_auth_retry_failures >= AUTH_RETRY_ESCALATION_THRESHOLD:
                                log_event("engine", "auth_retry_escalation_force_restart", tab_id=self.tab_id, chat_source=mode.chat_source)
                                self.consecutive_auth_retry_failures = 0
                                self.restart_pending = True
                                if self.client:
                                    await self._safe_disconnect(self.client)
                        continue

                    # --- session id capture ---
                    # Bug fix (2026-09-10): confirmed live -- tab 1 was stuck
                    # in a ~13s tool-use-concurrency restart loop, reusing the
                    # SAME condemned session id every time. Root cause: once
                    # _reset_unrecoverable_session() clears the tab's session
                    # id (self.last_saved_session_id = None) and schedules the
                    # client's disconnect in the background, this receive_messages()
                    # loop keeps running for a moment on the SAME still-alive
                    # client -- and its trailing messages still carry the OLD
                    # (now-condemned) session_id. Since last_saved_session_id
                    # had just become None, "sid != self.last_saved_session_id"
                    # was true again, so this block immediately re-saved the
                    # poisoned id, undoing the reset before the next restart
                    # even got a chance to start genuinely fresh. Guarded now:
                    # once a reset has been triggered this cycle, no further
                    # session_id from this doomed client is trusted.
                    sid = getattr(message, "session_id", None)
                    if sid and sid != self.last_saved_session_id and not self.restart_for_unrecoverable_session:
                        self.last_saved_session_id = sid
                        save_tab_session_id(self.workspace_dir, self.tab_id, sid, self.engine_kind)

                    if isinstance(message, ResultMessage):
                        # Bug fix (2026-09-15): the live "how much is
                        # actually in context right now" signal for the
                        # growth trigger (see tokens_at_last_forced_
                        # compaction's own __init__ comment) -- every real
                        # turn's own ResultMessage.usage already reports
                        # this, no separate query needed. Deliberately
                        # OUTSIDE the result_is_fake check below: even a
                        # hang-interrupted or otherwise "fake" turn's
                        # ResultMessage still reflects a real API call
                        # against the real current context, which is
                        # exactly the number this is meant to track --
                        # only the compaction command's OWN ResultMessage
                        # would be misleading here, and that's excluded
                        # explicitly (compaction's own token accounting
                        # comes from compact_boundary instead, see above).
                        usage = getattr(message, "usage", None)
                        if isinstance(usage, dict) and not self.forced_compaction_result_pending:
                            context_tokens = sum(
                                v for k, v in usage.items()
                                if k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                                and isinstance(v, int)
                            )
                            if context_tokens:
                                self.last_known_context_tokens = context_tokens
                        # Bug fix (2026-09-11): confirmed live -- a
                        # RateLimitEvent(rejected) sets
                        # ignore_next_result_recovery=True specifically so
                        # the trailing ResultMessage the SDK still sends
                        # for that same rejected turn doesn't get mistaken
                        # for a real completion -- but that flag only ever
                        # protected conn_state/the retry timer (below),
                        # never turn_pending/pending_user_text/silent_turn.
                        # Confirmed live: the rejected turn's own
                        # ResultMessage arrived ~2s after the rejection,
                        # cleared turn_pending/pending_user_text and set
                        # silent_turn=True -- so when the retry then fired
                        # 90s later and genuinely completed a real,
                        # substantive reply (present in the session
                        # transcript), that reply was silently swallowed
                        # for the reply's ENTIRE duration: the retry's own
                        # submit() call passes silent=True, and
                        # (True and True) stays True forever after, since
                        # nothing else was left to flip it back. Same bug
                        # class, same fix shape as hang_interrupt_result_pending
                        # (built for a hang-interrupt, a different
                        # trigger) -- captured together here, once, before
                        # ignore_next_result_recovery gets consumed a few
                        # lines down, so every place a ResultMessage
                        # touches turn state uses the SAME verdict.
                        #
                        # forced_compaction_result_pending (2026-09-11) joins
                        # the same verdict for the same reason: our own
                        # "/compact" produces its own ResultMessage that must
                        # not be shown, and must not clear turn_pending/
                        # pending_user_text -- especially since turn_pending
                        # was already False the whole time this ran (forced
                        # compaction only fires while idle), so touching it
                        # here would risk clobbering pending_user_text/
                        # pending_attachments for something ELSE that got
                        # queued in the meantime.
                        self._inflight_end()
                        if await self._ladder_on_result(message):
                            continue  # the attempt was rejected: the next model has the turn, nothing of this one is shown
                        result_is_fake = self.hang_interrupt_result_pending or self.ignore_next_result_recovery or self.forced_compaction_result_pending or self.clear_tab_result_pending
                        if not result_is_fake:
                            was_real_user_turn = self.pending_is_real_user
                            # Captured (and consumed) BEFORE the checks below can
                            # re-arm it for the NEXT turn -- see this flag's own
                            # __init__ comment for why it must never let its own
                            # nudge's reply re-trigger itself.
                            was_awaiting_post_turn_check_reply = self._awaiting_post_turn_check_reply
                            self._awaiting_post_turn_check_reply = False
                            self.turn_pending = False
                            # Reverted (2026-10-20), per explicit instruction, after a
                            # confirmed live incident: the 2026-10-19 hard-cancel here (see
                            # git history for its own full reasoning) killed a
                            # messenger_find_chat operation 1.2s after its own turn
                            # concluded -- the sidecar had ALREADY produced a real, correct
                            # result by then, but dispatch()'s own `await asyncio.wait_for
                            # (asyncio.shield(op.task), timeout=FAST_PATH_TIMEOUT_S)` was
                            # still inside its own fast-path window when the cancel arrived,
                            # so asyncio.CancelledError (a BaseException, not an Exception)
                            # propagated straight out of dispatch() uncaught -- no
                            # operation_running/operation_done/operation_error log line at
                            # all, and the model never learned the call had actually
                            # succeeded. It then fabricated a plausible-sounding but entirely
                            # invented error message instead of admitting it didn't know.
                            # The 2026-10-19 incident this cancel was meant to fix (10
                            # operations finishing within 15ms of each other, each firing an
                            # inject_proactive into a stuck compaction's own queue) is a real
                            # but narrower problem than "any operation outliving its own
                            # turn is bad" -- submit()'s own forced_compaction_result_pending
                            # check already queues a proactive notify safely instead of
                            # injecting it live, so the pile-up is now a batch of queued
                            # turns replayed in order once compaction ends, not live chaos.
                            # A walked-away operation is now left to finish on its own and
                            # report back through the EXISTING notify_on_completion ->
                            # _notify_unless_collected mechanism (NOTIFY_GRACE_S, the
                            # `collected` dedup flag) -- built for exactly this "model
                            # walked away" case. REGISTRY.cancel_for_tab is still called,
                            # unchanged, from ChatSession.stop() (an explicit user Stop is a
                            # genuine "abort everything in this tab now") and from clear_tab
                            # -- only this ordinary, successful-turn-conclusion call site is
                            # removed.
                            self.pending_user_text = None
                            self.pending_attachments = []
                            # Not for a disposed session (2026-10-26): its turn was interrupted by the
                            # window closing, not answered -- the pending turn must survive for the resume.
                            if not self.ended:
                                clear_pending_turn(self.workspace_dir, self.tab_id)
                            # Per explicit correction (2026-09-13): a real
                            # turn's genuine completion (not this fake/
                            # compaction/hang-recovery ResultMessage, and not
                            # an internal nudge answering itself) is exactly
                            # the moment to sanity-check whether the work is
                            # actually done -- see _fire_post_turn_completion_
                            # check's own doc comment for why this replaced a
                            # periodic timer instead.
                            #
                            # Bug fix (2026-09-18), per a real live incident:
                            # was_awaiting_post_turn_check_reply used to
                            # blanket-suppress EVERY completion that followed
                            # a check's own nudge, no matter what that
                            # completion actually contained -- confirmed live
                            # on a real multi-step task (checking a grant
                            # budget file for a typo): the check fired once,
                            # the nudge's reply turned into a whole further
                            # round of real work (multiple tool calls, real
                            # visible text: "Нашла реальную проблему...
                            # Ищу, где именно."), and THAT round then also
                            # ended mid-task with no further text -- but
                            # since it was "the check's reply", the guard
                            # blocked any further check forever, leaving the
                            # tab silently stuck until the user manually
                            # typed "и?" two minutes later to push it along.
                            # The guard's actual purpose (per its own
                            # original comment below) was only ever to stop
                            # an INFINITE TIGHT LOOP of a check chasing its
                            # own GENUINELY EMPTY reply -- a reply that did
                            # real, visible work is exactly the case that
                            # still needs checking again, same as a fresh
                            # real user turn. Re-arm on it, right alongside
                            # was_real_user_turn; still suppressed for a
                            # check-reply that came back with nothing at all
                            # (the elif below, unchanged for that case).
                            # A resumed real question must not end in silence (2026-09-23, live: a
                            # restart-interrupted question was "answered" with a bare [[NO_UPDATE]] the user
                            # never saw, so the tab looked idle until they typed "continue").
                            # A provider-rejected turn is not "finished but silent": re-asking "did you
                            # actually finish?" (or re-nudging a resumed question) against a request the
                            # provider keeps rejecting is what looped forever on a bad API key.
                            error_result = bool(getattr(message, "is_error", False))
                            resumed_nudged = False
                            if self.resumed_unanswered_question is not None and not error_result:
                                if self.turn_saw_any_visible_text:
                                    self.resumed_unanswered_question = None
                                    self.resumed_answer_nudges = 0
                                elif self.resumed_answer_nudges < RESUMED_ANSWER_MAX_NUDGES:
                                    self.resumed_answer_nudges += 1
                                    resumed_nudged = True
                                    log_event("engine", "resumed_question_silent_reply_renudged", tab_id=self.tab_id, attempt=self.resumed_answer_nudges)
                                    self.inject_proactive(
                                        "[The user's message below was interrupted by a restart and you have NOT yet shown them any "
                                        "visible answer to it -- they are waiting and currently see nothing. A reply of [[NO_UPDATE]] "
                                        "is not allowed here. Answer it now, in "
                                        f"{current_language_name(self.tab_id)}: if the work was already completed, say what was done and "
                                        "where the result is (check first if you're unsure); if it still needs doing, do it. Never "
                                        "mention the restart itself.\n\nThe user's original message:\n"
                                        f'"{self.resumed_unanswered_question}"]',
                                        pending_text=self.resumed_unanswered_question,
                                    )
                                else:
                                    log_event("engine", "resumed_question_renudge_exhausted", tab_id=self.tab_id)
                                    self.resumed_unanswered_question = None
                                    self.resumed_answer_nudges = 0
                            if resumed_nudged:
                                pass
                            elif error_result:
                                log_event("engine", "post_turn_completion_check_skipped_error_result", tab_id=self.tab_id)
                            elif was_real_user_turn or (was_awaiting_post_turn_check_reply and self.turn_saw_any_visible_text):
                                self._fire_post_turn_completion_check()
                            elif not was_awaiting_post_turn_check_reply and not self.turn_saw_any_visible_text:
                                # Per explicit instruction (2026-09-13), after a
                                # real incident: a PROACTIVE turn (a scheduled
                                # mailbox check) completed with genuinely EMPTY
                                # visible content -- real work done, real output
                                # tokens spent, but no text (not even
                                # [[NO_UPDATE]]) ever reached the wire. Most
                                # proactive completions are legitimately silent
                                # via a real NO_UPDATE text block (which DOES
                                # count as "visible" here, see turn_saw_any_
                                # visible_text's own comment) -- this only fires
                                # for the genuinely-empty case; a check-reply
                                # that's ALSO genuinely empty stops here (see
                                # the branch above for the non-empty case),
                                # never chaining into an infinite tight loop of
                                # a check chasing its own silence.
                                log_event("engine", "post_turn_completion_check_proactive_empty", tab_id=self.tab_id)
                                self._fire_post_turn_completion_check()
                            elif was_awaiting_post_turn_check_reply and not self.turn_saw_any_visible_text and self._turn_has_unresolved_tool_error():
                                # Ground-truth override (2026-09-22), per
                                # explicit instruction that Caroline must
                                # push through to a real answer rather than
                                # formally "end a turn": neither branch
                                # above caught this case -- a bare
                                # [[NO_UPDATE]] check-reply (stripped before
                                # turn_saw_any_visible_text is ever set, see
                                # _strip_no_update_from_wire's own call
                                # site) would otherwise be accepted at face
                                # value and silently drop the check here.
                                # But this episode's own ground truth (see
                                # ToolOutcome/turn_tool_outcomes) shows a
                                # tool call that's still sitting at
                                # is_error=True -- the model's silence is
                                # not evidence the work is actually done, so
                                # don't trust it; re-fire the same check
                                # again, now carrying that ground truth (see
                                # _fire_post_turn_completion_check's own
                                # _turn_outcomes_summary() call), same flat
                                # re-ask-forever shape every other internal
                                # nudge here already uses.
                                log_event("engine", "post_turn_completion_check_ground_truth_override", tab_id=self.tab_id)
                                self._fire_post_turn_completion_check()
                            if was_real_user_turn and not resumed_nudged:
                                await self._check_refusal_and_maybe_retry()
                        self.classifier_refusal_retry_count = 0
                        self.last_api_retry_error = None
                        self.consecutive_auth_retry_failures = 0
                        # Bug fix (2026-09-10): confirmed live -- a
                        # RateLimitEvent(rejected) schedules a 90s retry
                        # timer and sets ignore_next_result_recovery=True
                        # specifically so the ResultMessage the SDK still
                        # sends for that same failed turn doesn't get
                        # mistaken for real recovery. That flag protected
                        # conn_state (stayed "limited", correctly) but NOT
                        # this retry timer -- _clear_api_retry_timer() ran
                        # unconditionally right here and silently cancelled
                        # the just-scheduled retry in the same breath it
                        # was created. Nothing ever rescheduled a fresh
                        # one, so the tab sat in "limited" forever with a
                        # dead retry mechanism (confirmed live: 3/3 real
                        # incidents show reset_at.reschedule.reset_at with
                        # no api_retry_firing ever following). Now the
                        # whole "this ResultMessage isn't real recovery"
                        # branch, including the retry timer, is gated on
                        # the SAME flag.
                        if self.ignore_next_result_recovery:
                            self.ignore_next_result_recovery = False
                        else:
                            self._clear_api_retry_timer()
                            if self.suppress_next_conn_state_reset:
                                self.suppress_next_conn_state_reset = False
                            elif self.conn_state.get("kind") != "connected":
                                self._set_conn_state("connected")

                    wire = message_to_wire(message)
                    self._ladder_observe(wire)
                    self._dr_observe(wire)
                    if not isinstance(message, ResultMessage) and self._ladder_hold(wire):
                        wire = None  # kept for the verdict of the model ladder (_ladder_on_result sends it)
                    # Bug fix (2026-09-10/11): an interrupted or rejected
                    # turn's own ResultMessage carries whatever partial/
                    # empty/error result the CLI had at that moment --
                    # never something to show as if it were Caroline's
                    # real answer. Suppressing the send (not just the
                    # turn_pending clear above) also keeps chat.js's
                    # turnQueue placeholder for the ORIGINAL real submit
                    # open until the REPLAY's own genuine ResultMessage
                    # resolves it for real, instead of being wrongly
                    # resolved early by this one. (silent_turn itself
                    # removed 2026-09-11, per explicit instruction -- it
                    # was a whole-session AND-latch that couldn't be
                    # un-set once a real conversation had made it False,
                    # and its own state got corrupted by exactly this kind
                    # of fake-ResultMessage bug. Whether a REAL message
                    # gets shown is now a per-message [[NO_UPDATE]]
                    # decision the model itself makes -- this check here
                    # is now purely about not sending a KNOWN-meaningless
                    # fake result.
                    #
                    # forced_compaction_result_pending (2026-09-11) needs its
                    # OWN, broader term here rather than folding into
                    # result_is_fake's usual ResultMessage-only check: a
                    # real "/compact" round trip also produces real
                    # AssistantMessage/SystemMessage traffic BEFORE its
                    # ResultMessage (live-confirmed: an assistant reply like
                    # "Not enough messages to compact.", plus a "compacting"
                    # status message) that would otherwise show up as a
                    # bogus chat bubble -- suppress the whole stretch, not
                    # just the terminal ResultMessage.)
                    if wire is not None and not self.forced_compaction_result_pending and not (isinstance(message, ResultMessage) and result_is_fake):
                        wire = _strip_no_update_from_wire(wire, self.tab_id)
                        if wire is not None:
                            wire = await self._translate_wire_visible_text(wire)
                            if wire is not None:
                                # Per explicit instruction (2026-10-13): reply.review (inside
                                # _translate_wire_visible_text) can itself decide a block is
                                # pure internal-mechanics and rewrite it down to [[NO_UPDATE]]/a
                                # silent-reply paraphrase -- see that function's own docstring.
                                # It returns None in exactly that case, same contract as
                                # _strip_no_update_from_wire above; this mirrors that function's
                                # own None-check rather than assuming wire is always still a dict.
                                #
                                # Bug fix (2026-09-10): confirmed live -- voice-reply
                                # auto-play/animation (chat.js checks evt.isVoice on
                                # the "result" event) never fired, because this send
                                # site never attached isVoice at all. server.ts's
                                # original does: { type: "sdk_message", message,
                                # isVoice: this.turnIsVoice } specifically on the
                                # "result" message -- ported that shape here; every
                                # other message type is sent exactly as before.
                                #
                                # Bug fix (2026-09-13), confirmed live from a real
                                # incident: a voice-originated turn that ran long
                                # (several minutes of tool calls) DID produce a real,
                                # visible mid-turn text reply (shown as a chat bubble)
                                # -- but the SDK's own transport then hit an unrelated
                                # failure ("JSON message exceeded maximum buffer size")
                                # before ever reaching a clean ResultMessage, so
                                # isVoice (previously attached ONLY to "result")
                                # never reached the client at all -- nothing was ever
                                # spoken/animated, even though the user had already
                                # SEEN a real reply. Voice/Visual-Mode playback must
                                # not depend on the turn eventually reaching a clean
                                # result -- attach isVoice to EVERY sdk_message now
                                # (self.turn_is_voice is stable for the whole logical
                                # turn, including across a crash-triggered replay --
                                # see its own "leave untouched"/"preserved" comments
                                # below), so chat.js can speak/animate each visible
                                # reply as it actually arrives.
                                envelope: dict[str, Any] = {"type": "sdk_message", "message": wire, "isVoice": self.turn_is_voice}
                                if wire.get("type") == "result":
                                    # Distinguishes a genuine turn's settlement from
                                    # OUR OWN internal post-turn completion check
                                    # re-asking itself (was_awaiting_post_turn_check_
                                    # reply, captured above -- see
                                    # _fire_post_turn_completion_check). A caller
                                    # that cares whether a settlement represents a
                                    # real externally-triggered turn (e.g. the
                                    # Ratatosk owner-channel's "did this actually
                                    # get replied to" fallback, app/main.py's
                                    # _ratatosk_session_send) needs this signal --
                                    # without it, a legitimately-silent internal
                                    # check-reply (a bare [[NO_UPDATE]] answering
                                    # our own nudge, not the owner) looks identical
                                    # to a real turn that genuinely never got
                                    # replied to. Nothing else currently reads it.
                                    envelope["wasInternalCheckReply"] = was_awaiting_post_turn_check_reply
                                await self.send(envelope)
                                if wire.get("type") in ("assistant", "result"):
                                    if not self.real_user_turn_answered:
                                        self.real_user_turn_answered = True
                                    if wire.get("type") == "assistant":
                                        # Bug fix (2026-09-13), confirmed live from the
                                        # real session transcript (tab 1, "Основной
                                        # диалог", 2026-09-13 ~14:04-14:08 UTC): dozens
                                        # of consecutive assistant wire messages during
                                        # a long multi-mailbox tool-calling stretch were
                                        # tool_use-ONLY (no text block) -- chat.js's own
                                        # hasToolUse check (mirrored server-side in
                                        # history.py's _extract_entries_from_jsonl)
                                        # suppresses exactly these as pre-tool narration
                                        # the user never sees. This line used to bump
                                        # last_visible_output_at on EVERY one of them
                                        # regardless, repeatedly re-arming
                                        # _check_progress_narration()'s 60s cooldown
                                        # without the user ever actually seeing anything
                                        # new -- the confirmed cause of a 5+ minute
                                        # narrator silence with real work still running.
                                        # Only a message carrying an actual visible text
                                        # block should count as "the user just saw
                                        # something new".
                                        content_blocks = wire.get("message", {}).get("content") or []
                                        has_visible_text = any(
                                            isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
                                            for b in content_blocks
                                        )
                                        has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content_blocks)
                                        log_event(
                                            "engine", "assistant_wire_sent", tab_id=self.tab_id,
                                            has_visible_text=has_visible_text, has_tool_use=has_tool_use,
                                            last_visible_output_updated=has_visible_text,
                                        )
                                        if has_visible_text:
                                            self.last_visible_output_at = time.monotonic()
                                            self.turn_saw_any_visible_text = True
                                            self.consecutive_narration_count = 0
                                            # Refusal-guard's own input (see
                                            # _check_refusal_and_maybe_retry) --
                                            # only for a real user turn; a
                                            # proactive/internal one declining
                                            # something is a different, not-yet
                                            # in-scope case.
                                            if self.pending_is_real_user:
                                                self._turn_last_visible_text = "\n".join(
                                                    b.get("text", "") for b in content_blocks
                                                    if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
                                                )

                    if isinstance(message, ResultMessage):
                        if result_is_fake:
                            # Consumed here (not earlier) -- this is the
                            # last of the three places this ResultMessage
                            # touches result_is_fake-gated state. Leave
                            # turn_is_voice untouched too (not reset to
                            # False here) -- preserved for whichever REAL
                            # ResultMessage eventually completes this same
                            # logical turn.
                            self.hang_interrupt_result_pending = False
                            if self.forced_compaction_result_pending:
                                self.forced_compaction_result_pending = False
                                log_event("engine", "forced_compaction_done", tab_id=self.tab_id, subtype=message.subtype)
                                # Per explicit instruction (2026-09-18): the
                                # status-bar yellow dot (_compute_public_
                                # status) and anything else that treats
                                # forced_compaction_result_pending as "still
                                # compacting" needs to hear about this edge
                                # THE MOMENT it happens, not whenever the
                                # next unrelated turn_pending flip republishes
                                # status -- compaction touches neither.
                                asyncio.create_task(self._publish_status())
                                self._drain_compaction_queue()
                            elif self.clear_tab_result_pending:
                                self.clear_tab_result_pending = False
                                log_event("engine", "clear_tab_result_discarded", tab_id=self.tab_id, subtype=message.subtype)
                            else:
                                log_event("engine", "fake_result_message_preserved", tab_id=self.tab_id)
                        else:
                            # One long-lived client now -- the turn is done,
                            # but the stream stays open for the next queued
                            # turn (_input_stream yields it). No per-turn
                            # restart.
                            self.turn_is_voice = False
                            self._dr_finish(message)

                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        self.has_seen_init = True
                        self.connecting_since = None
                        log_event("engine", "init_received", tab_id=self.tab_id, elapsed_ms=round((time.monotonic() - query_started_at) * 1000), resume=resume_session_id)
                        if self.conn_state.get("kind") != "connected":
                            self._set_conn_state("connected")
                        # A failed server here behaves the same way it would in
                        # an interactive `claude` session: unavailable, but
                        # everything else still works -- previously this would
                        # have had to throw and tear down the WHOLE session to
                        # retry, which just re-fails the same way on every
                        # restart for an unrelated server (an infinite
                        # "recovering session..." loop). 'needs-auth'/'pending'/
                        # 'disabled' aren't retried here (an auth prompt, a
                        # race that may still resolve, or intentional), only
                        # 'failed' is.
                        failed_servers = [s.get("name") for s in (message.data.get("mcp_servers") or []) if s.get("status") == "failed"]
                        if failed_servers:
                            log_event("engine", "mcp_servers_failed", tab_id=self.tab_id, servers=failed_servers)
                            for server_name in failed_servers:
                                if self.client and server_name:
                                    self._schedule_mcp_reconnect(self.client, server_name)
                        # See dynamic_disallowed_tools' own comment above --
                        # this init message's own "tools" field is the real,
                        # live ground truth for what's actually available THIS
                        # connection (own and foreign alike); persisted so the
                        # NEXT query build (not this already-in-flight one --
                        # disallowed_tools is already locked in for it) closes
                        # the loop instead of staying wrong forever.
                        overlap = compute_foreign_tool_overlap(message.data.get("tools") or [], set(mcp_servers.keys()))
                        save_discovered_foreign_tool_overlap(self.workspace_dir, overlap)
                        if overlap:
                            log_event("engine", "foreign_tool_overlap_discovered", tab_id=self.tab_id, tools=overlap)

                if not self.ended:
                    log_event("engine", "stream_ended_unexpectedly", tab_id=self.tab_id, elapsed_ms=round((time.monotonic() - query_started_at) * 1000), has_seen_init=self.has_seen_init)
                    raise RuntimeError("query() stream ended unexpectedly")

            except Exception as exc:  # noqa: BLE001 -- must classify, not swallow
                if self.ended:
                    return

                if self._stop_killed_idle_engine:
                    # Stop pressed between turns killed an idle engine (see
                    # stop()). Nothing was in flight, so there is nothing to
                    # tell the model and no turn to start: just come back up
                    # with a fresh engine and wait for the user.
                    self._stop_killed_idle_engine = False
                    self.user_stop_requested = False
                    log_event("engine", "restarting_engine_after_stop_between_turns", tab_id=self.tab_id)
                    self._reset_after_stop()
                    continue

                if self.user_stop_requested:
                    self.user_stop_requested = False
                    # Bug fix (2026-10-09), confirmed live: this used to
                    # submit a "[The user just stopped what you were doing...]"
                    # note as a turn of its own -- so every Stop started a new
                    # turn 20-55 s later, the moment the engine came back up,
                    # and the model went on working. Now nothing is started:
                    # the note rides on the user's next real message instead
                    # (see _stop_note_pending in submit()).
                    self._reset_after_stop()
                    continue

                if self.restart_pending:
                    self.restart_pending = False
                    replay_text = self.pending_user_text
                    replay_attachments = self.pending_attachments
                    log_event("engine", "restart_pending_handled", tab_id=self.tab_id, has_replay=replay_text is not None)
                    self.last_rate_limit_info = None
                    self.turn_pending = False
                    self.pending_user_text = None
                    self.pending_attachments = []
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    if replay_text is not None:
                        self.submit(replay_text, replay_attachments, True, self.turn_is_voice)
                    continue

                if self.restart_for_unrecoverable_session:
                    self.restart_for_unrecoverable_session = False
                    replay_text = self.unrecoverable_session_replay_text
                    replay_attachments = self.unrecoverable_session_replay_attachments
                    self.unrecoverable_session_replay_text = None
                    self.unrecoverable_session_replay_attachments = []
                    log_event("engine", "restart_unrecoverable_session", tab_id=self.tab_id, has_replay=replay_text is not None)
                    self.turn_pending = False
                    self.pending_user_text = None
                    self.pending_attachments = []
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    if replay_text is not None:
                        # No per-call directive text needed (2026-09-09
                        # redesign) -- the fresh query() this replay lands in
                        # already carries language_hint_instruction
                        # (current_language_name(), synchronous, instant) in
                        # its own system prompt. Just kick off a background
                        # refresh for the next reset/turn.
                        refresh_language_in_background(self.last_saved_session_id, self.tab_id)
                        self.submit(replay_text, replay_attachments, True, self.turn_is_voice)
                    continue

                balance_source = detect_balance_exhaustion(str(exc))
                if balance_source:
                    log_event("engine", "thrown_balance_exhaustion", tab_id=self.tab_id, source=balance_source)
                    explanation = self._handle_balance_exhausted()
                    self.turn_pending = False
                    self.pending_user_text = None
                    self.pending_attachments = []
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    self._set_conn_state("billing_blocked", explanation)
                    self._schedule_api_retry(f"billing_error(thrown):{balance_source}", self.turn_is_voice)
                    continue

                if self.last_api_retry_error == "rate_limit":
                    log_event("engine", "silent_death_rate_limit", tab_id=self.tab_id)
                    self.turn_pending = False
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    self._set_conn_state("limited", "Hit the Claude usage limit. Retrying automatically.")
                    self._schedule_api_retry("api_retry:rate_limit", self.turn_is_voice)
                    continue

                if self.last_rate_limit_info and self.last_rate_limit_info.get("status") == "rejected":
                    log_event("engine", "silent_death_rate_limit_rejected", tab_id=self.tab_id)
                    self.turn_pending = False
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    self._handle_rate_limit_rejected("silent-stream-death", self.last_rate_limit_info)
                    continue

                # Per explicit instruction (2026-09-13): raising
                # max_buffer_size (options_kwargs, above) is a ceiling, not
                # a fix -- confirmed live that blindly replaying the exact
                # same request after this failure just repeats the exact
                # same expensive approach (reading several full-resolution
                # images via the native Read tool back-to-back) and can
                # cross even a much higher ceiling. A dedicated note is
                # needed so the model actually changes approach, not just
                # retries -- generic _handle_failure's own {exc} text alone
                # gives it nothing to act on differently. No manual
                # transcript surgery to "delete" the oversized result (the
                # CLI already wrote it to the resumed session's own
                # transcript before this failure fired; editing that file
                # out from under a resuming CLI is not safe) -- the EXISTING
                # forced-compaction-on-growth check (_check_forced_
                # compaction, 100KB threshold) already dehydrates it out to
                # a file + pointer automatically within one watchdog tick
                # of the replayed turn actually completing, which is
                # exactly the "write to a file and point at it" shape this
                # was missing -- it just needs THIS retry to succeed rather
                # than loop forever repeating the same failure.
                if OVERSIZED_MESSAGE_PATTERN.search(str(exc)):
                    log_event("engine", "oversized_tool_result_failure", tab_id=self.tab_id, error=str(exc))
                    await self._handle_failure(
                        exc,
                        extra_note=(
                            "The failure was a single tool result too large to process (most likely reading "
                            "several full-resolution images in a row with the Read tool). Don't repeat that same "
                            "approach: process images one at a time rather than in a burst, and prefer whatever "
                            "cheaper/lower-resolution option is available (a thumbnail, a smaller crop, a text "
                            "description of the image) over reading multiple large full-resolution files back to "
                            "back."
                        ),
                    )
                    continue

                await self._handle_failure(exc)
        # edition:end
        #ic| # The IntelliCat edition answers through SquirrelWisdom only: a tab builds no agent session, it only reports healthy.
        #ic| self.client = None
        #ic| self._set_conn_state("connected")
        #ic| while not self.ended:
        #ic|     await asyncio.sleep(5.0)

from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class StatusConnMixin:
    @property
    def turn_pending(self) -> bool:
        return self._turn_pending
    @turn_pending.setter
    def turn_pending(self, value: bool) -> None:
        if value == self._turn_pending:
            return
        log_event("engine", "turn_pending_changed", tab_id=self.tab_id, prev=self._turn_pending, new=value)
        self._turn_pending = value
        # Bug fix (2026-09-16), per explicit instruction: _check_hang's
        # process-activity signal (2026-09-15) has no absolute ceiling of
        # its own -- a process that keeps showing SOME CPU/RSS/IO movement
        # at least once every <90s (its own periodic retries, background
        # chatter, anything) is "alive" by that definition forever, no
        # matter how long the actual turn has gone unanswered. Confirmed
        # live: a real turn sat pending for 15.5 minutes with hang_count
        # staying at 0 the entire time. This timestamp -- when the CURRENT
        # turn actually started, independent of the activity signal -- is
        # what lets _check_hang enforce a hard ceiling on top of it.
        self.turn_pending_since = time.monotonic() if value else None
        # Every one of the ~9 places in this file that flips turn_pending
        # now republishes status automatically -- no call site has to
        # remember to do it itself (that "remember to do it everywhere"
        # pattern is exactly what produced tonight's whole run of bugs).
        asyncio.create_task(self._publish_status())
    def _check_funds_exhaustion_status(self) -> None:
        """Per explicit instruction (2026-09-16): sw_api.py's exhaustion
        flag is set/cleared as a side effect of whatever REAL call
        happened to notice it (a narration tick, a translation, ...) --
        nothing proactively tells THIS tab's own status bar when it
        changes on its own. Polled once per watchdog tick (cheap, no
        network call -- just reads a module-level string) so a change
        reaches the status bar within one tick either direction, not only
        the next time something else happens to trigger a republish."""
        current = get_funds_exhausted_reason()
        if current != self._last_known_funds_exhausted_reason:
            was_blocked = bool(self._last_known_funds_exhausted_reason)
            self._last_known_funds_exhausted_reason = current
            asyncio.create_task(self._publish_status())
            # Per explicit instruction (2026-10-15): the transition TO None
            # is what unblocks submit() again -- see funds_blocked_queued_
            # turns' own __init__ comment. sw_api.py's own probe loop (not
            # this tick) is what actually makes that transition happen
            # while everything is paused; this just has to notice it.
            if was_blocked and not current:
                self._drain_funds_blocked_queue()
    def _compute_public_status(self) -> tuple[str, str]:
        kind = self.conn_state.get("kind")
        reason = self.conn_state.get("reason") or ""
        if kind in ("billing_blocked", "not_logged_in", "auth_failed"):
            return "error", reason
        if kind in ("restarting", "restart_backoff", "limited", "engine_error"):
            return "recovering", reason
        # Bug fix (2026-09-16), originally: "при исчерпании баланса на
        # клоде или опенроутере эта информация явно прокидывалась в
        # кэролайн и высвечивалась на статус-баре" -- see
        # sw_api.get_funds_exhausted_reason's own comment for how this is
        # detected. Used to ride along as extra reason text on ready/
        # working, deliberately not blocking (an exhausted balance didn't
        # stop a tab using its own Claude subscription, only narration/
        # translation/SW-mode chat).
        # Superseded (2026-10-15), per explicit instruction, a real
        # incident: "При недостатке баланса у провайдера ВСЯ деятельность
        # Кэролайн должна ПОЛНОСТЬЮ блокироваться до пополнения баланса" --
        # submit() now queues every new turn instead of starting it while
        # this is set (see funds_blocked_queued_turns' own __init__
        # comment), so this has to be a real blocking state now, same
        # "recovering" yellow lamp as restarting/limited/compacting above,
        # not just a note riding along on ready/working -- the old
        # ready/working return below would otherwise claim the tab is fine
        # while it's actually refusing every new turn.
        funds_reason = get_funds_exhausted_reason()
        if funds_reason:
            queued_note = f" ({len(self.funds_blocked_queued_turns)} message(s) queued)" if self.funds_blocked_queued_turns else ""
            return "recovering", f"Paused -- the AI provider's balance is exhausted, waiting for it to be topped up ({funds_reason}){queued_note}"
        # Per explicit instruction (2026-09-18): compaction must be visible
        # on the status bar (the yellow "recovering" lamp, same as
        # restarting/restart_backoff/limited above) -- previously silent,
        # since forced compaction deliberately bypasses turn_pending
        # entirely (see _check_forced_compaction's own docstring) and
        # nothing else was publishing a status change for it. Checked
        # before turn_pending: a real message submitted during compaction
        # is now queued (see compaction_queued_turns), not pushed, so
        # turn_pending itself stays False throughout -- this is the ONLY
        # signal that would otherwise tell the user anything is happening.
        if self.forced_compaction_result_pending:
            queued_note = f" ({len(self.compaction_queued_turns)} message(s) queued)" if self.compaction_queued_turns else ""
            return "recovering", f"Compacting conversation, one moment…{queued_note}"
        if self.turn_pending or self._inflight_active():
            return "working", ""
        return "ready", ""
    # Messages handed to the CLI whose result has not come back (2026-10-26): turn_pending is cleared by the FIRST result, so a
    # tab that still had queued internal messages (a finished background task, a check) was "ready" -- no Stop button, a tool
    # label left in the status bar -- while the model went on working. The status is "working" while any is in flight, unless
    # nothing has been heard from the CLI for INFLIGHT_STALE_S (a result that never comes must not leave it "working").
    _inflight_messages: int = 0
    _inflight_since: float = 0.0
    _last_public_state: str | None = None
    INFLIGHT_STALE_S = 45.0
    INFLIGHT_SETTLE_S = 8.0

    def _inflight_active(self) -> bool:
        if self._inflight_messages <= 0:
            return False
        return time.monotonic() - max(self._inflight_since, getattr(self, "last_activity", 0.0) or 0.0) < self.INFLIGHT_STALE_S

    # edition:cut claude-session code no longer reached
    def _inflight_begin(self) -> None:
        self._inflight_messages += 1
        self._inflight_since = time.monotonic()
        self._publish_status_if_changed()
    # edition:end

    # edition:cut claude-session code no longer reached
    def _inflight_end(self) -> None:
        self._inflight_messages = max(0, self._inflight_messages - 1)
        self._publish_status_if_changed()
        if self._inflight_messages > 0:
            # The CLI starts the next message it holds within moments of a result. Merged messages leave no result of their
            # own, so if nothing at all is heard for INFLIGHT_SETTLE_S, nothing is in flight any more.
            marker = time.monotonic()
            try:
                asyncio.get_running_loop().call_later(self.INFLIGHT_SETTLE_S, self._inflight_settle, marker)
            except RuntimeError:
                pass
    # edition:end

    # edition:cut claude-session code no longer reached
    def _inflight_settle(self, marker: float) -> None:
        if self._inflight_messages > 0 and (getattr(self, "last_activity", 0.0) or 0.0) - marker < 0.5:
            self._inflight_messages = 0
            self._publish_status_if_changed()
    # edition:end

    # edition:cut claude-session code no longer reached
    def _inflight_reset(self) -> None:
        self._inflight_messages = 0
    # edition:end

    def _publish_status_if_changed(self) -> None:
        try:
            state, _reason = self._compute_public_status()
            if state != self._last_public_state:
                self._last_public_state = state          # at once: two calls in a row must not send the same state twice
                asyncio.get_running_loop().create_task(self._publish_status())
        except RuntimeError:
            pass          # no running loop (a plain script or test): nothing to publish to

    async def _publish_status(self) -> None:
        state, reason = self._compute_public_status()
        self._last_public_state = state
        # Per standing instruction ("логи повсеместно"): every status the
        # client is told about is logged here, in ONE place, regardless of
        # which of the several call sites (turn_pending's own setter,
        # _apply_conn_state, the initial connect in main.py) triggered it --
        # matches the same "log at the one choke point, not at every
        # caller" shape as task_supervisor.py.
        log_event("engine", "status_published", tab_id=self.tab_id, state=state, reason=reason)
        # "kind" is additive (chat.js's lamp logic only ever reads "state"/
        # "reason", unaffected) -- lets chat.js tell "not_logged_in" apart
        # from "billing_blocked" even though both publish as state="error",
        # so it can raise a one-time native dialog only for the former (see
        # chat.js's applyCarolineStatus).
        await self.send({"type": "status", "state": state, "reason": reason, "kind": self.conn_state.get("kind")})
    async def _apply_conn_state(self, kind: str, reason: str | None = None) -> None:
        # Bug fix (2026-09-11), per explicit instruction: used to send one
        # of TWO different wire message types (caroline_status/
        # system_notice) depending on kind, each carrying its own ad-hoc
        # status text/cls -- chat.js then had to reconstruct "is this
        # actually working, waiting, or broken" from THAT, PLUS turnQueue
        # length, PLUS wsConnected, independently. Collapsed to a single
        # _publish_status() call -- see _compute_public_status for the
        # kind -> READY/WORKING/RECOVERING/ERROR mapping.
        prev_kind = self.conn_state.get("kind")
        self.conn_state = {"kind": kind, "reason": reason}
        log_event("engine", "conn_state", tab_id=self.tab_id, prev=prev_kind, new=kind, reason=reason)
        # Bug fix (2026-09-15), per explicit instruction: confirmed live --
        # a forced-compaction attempt that itself hits the usage cap (the
        # CLI's own "Error during compaction: You've hit your session
        # limit" message) gets classified through the generic CC-CLI-
        # limit-message path, which never resets forced_compaction_result_
        # pending (that only happens in the normal ResultMessage handler,
        # which this classification path skips via its own `continue`).
        # Left stuck at True forever after that, _check_forced_compaction's
        # own first guard blocks EVERY future attempt -- even once the cap
        # resets, nothing ever retries, so a context that grew large enough
        # to need compaction in the first place (confirmed live: ~48MB of
        # accumulated Read-tool image results) never actually shrinks.
        # Coming out of "limited" is exactly the moment to re-check: set a
        # dedicated flag here (consumed by _check_forced_compaction, which
        # also bypasses the normal cooldown for it -- the point is
        # specifically to retry what the cap just interrupted, not wait for
        # the next regular cycle) and clear the stuck flag too, in case
        # that's what actually got stuck.
        if prev_kind == "limited" and kind != "limited":
            self.needs_post_limit_compaction_check = True
            if self.forced_compaction_result_pending:
                self.forced_compaction_result_pending = False
                log_event("engine", "forced_compaction_result_pending_cleared_after_limit", tab_id=self.tab_id)
                self._drain_compaction_queue()
        await self._publish_status()
    def _set_conn_state(self, kind: str, reason: str | None = None, arm_ignore_next_result: bool = False) -> None:
        if arm_ignore_next_result:
            self.ignore_next_result_recovery = True
        asyncio.create_task(self._apply_conn_state(kind, reason))
    def has_live_dialog(self, idle_threshold_s: float = 5 * 60) -> bool:
        """Bug fix (2026-09-10): confirmed live -- deleted along with the
        homegrown compaction loop (its only OTHER caller), but main.py's
        _on_reminder_due still calls this to decide whether a "background"
        priority reminder (e.g. the hourly vault-backup nudge) should wait
        for the user to go quiet instead of interrupting a live turn."""
        if self.conn_state.get("kind") == "restart_backoff":
            return False
        return self.turn_pending or (time.monotonic() - self.last_user_activity) < idle_threshold_s
    def status(self) -> dict[str, Any]:
        return {
            "tabId": self.tab_id,
            "ended": self.ended,
            "turnPending": self.turn_pending,
            "hasSeenInit": self.has_seen_init,
            "lastActivityMs": round((time.monotonic() - self.last_activity) * 1000),
            "lastUserActivityMs": round((time.monotonic() - self.last_user_activity) * 1000),
            "hangCount": self.hang_count,
            "connState": self.conn_state,
            # Per explicit instruction (2026-09-13): App.xaml.cs's startup
            # splash must not dismiss (and the main window must not even be
            # shown) while any tab's forced startup compaction is still
            # running -- see WaitForSplashDismissAsync's own doc comment for
            # why _check_forced_compaction's own "startup" trigger otherwise
            # raced the splash-dismiss/SyncTabListToBackend timing and
            # produced a window with a missing tab list.
            "forcedCompactionPending": self.forced_compaction_result_pending,
            # Bug fix (2026-09-27), found while building the Python supervisor
            # (see backend-py/supervisor.py): MainWindow.RecoverTab (formerly
            # C#, now that supervisor's per-tab watchdog) reads this exact key
            # to know which OS process to kill for a stuck tab WITHOUT taking
            # the whole backend down -- it was never actually populated here,
            # so per-tab recovery silently no-opped every time ("no
            # cliProcessPid known for this tab yet") no matter how long a turn
            # had been stuck. self._cli_process_pid is captured at spawn time
            # (win_subprocess_patch.py's pid sink) -- just never surfaced.
            "cliProcessPid": self._cli_process_pid,
            # How long the CLI process itself has shown no sign of work
            # (CPU/RSS/IO, see process_activity.py) -- the same signal
            # _check_hang judges hangs by; None when it cannot be read.
            # lastActivityMs above only counts SDK messages, and a model
            # writing one long reply sends none for minutes (bug fix
            # 2026-10-10: the supervisor killed exactly such a turn).
            "processIdleMs": self._process_idle_ms(),
        }
    def _process_idle_ms(self) -> int | None:
        monitor = self._process_activity_monitor
        if monitor is None or not monitor.available:
            return None
        return round(monitor.seconds_since_last_activity() * 1000)

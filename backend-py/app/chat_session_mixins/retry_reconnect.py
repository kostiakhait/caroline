from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class RetryReconnectMixin:
    def _clear_api_retry_timer(self, clear_persisted: bool = True) -> None:
        """`clear_persisted=False` only from dispose(): a retry timer
        cancelled because the PROCESS is tearing down (app closing,
        restarting) must stay recorded on disk -- see durability.py's own
        "pending API retry" section for the 2026-10-12 incident this
        covers. Every other caller (Stop, an engine switch, a genuine
        recovery, or _fire() itself once it has actually injected the
        nudge) means the retry is genuinely no longer owed, so the default
        clears the durability file too -- callers never have to remember
        to do it themselves."""
        if self.api_retry_timer:
            self.api_retry_timer.cancel()
            self.api_retry_timer = None
        if clear_persisted:
            clear_pending_api_retry(self.workspace_dir, self.tab_id)
    # edition:cut claude-session code no longer reached
    def _schedule_api_retry(self, reason: str, is_voice: bool) -> None:
        if self._stopped_by_user("api_retry", reason):
            return
        if self.api_retry_timer:
            log_event("engine", "api_retry_already_pending", tab_id=self.tab_id, reason=reason)
            return
        log_event("engine", "api_retry_scheduled", tab_id=self.tab_id, reason=reason, delay_ms=API_RETRY_INTERVAL_MS)
        # Per explicit instruction (2026-10-12): survives a full app restart --
        # see durability.py's own "pending API retry" section for the real
        # incident this closes. Cleared by _clear_api_retry_timer (every
        # caller of that except dispose()) or right below once this _fire()
        # has actually injected the nudge.
        save_pending_api_retry(self.workspace_dir, self.tab_id, reason, is_voice)

        def _fire() -> None:
            self.api_retry_timer = None
            if self.ended:
                return
            log_event("engine", "api_retry_firing", tab_id=self.tab_id, reason=reason)
            # Bug fix (2026-09-11): confirmed live -- this used to call
            # submit() directly, bypassing inject_proactive() (the only
            # thing that sends proactive_turn_queued, which re-arms
            # chat.js's busy lamp/heartbeat) -- so from the moment a
            # rate-limit hit to whenever the retry's own first assistant
            # message happened to trigger chat.js's lazy fallback, there
            # was no "working" indication at all. Also reuses
            # CONTINUE_OR_SILENT_NUDGE_TEMPLATE now (language-aware,
            # [[NO_UPDATE]]-aware) instead of bespoke text with neither.
            clear_pending_api_retry(self.workspace_dir, self.tab_id)
            self.inject_proactive(
                "[Internal: automatic recheck after an API/subscription limit blocked a previous turn.] "
                f"{CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=current_language_name(self.tab_id))}",
                is_voice,
            )

        loop = asyncio.get_event_loop()
        self.api_retry_timer = loop.call_later(API_RETRY_INTERVAL_MS / 1000, _fire)
    # edition:end
    # edition:cut claude-session code no longer reached
    def _schedule_one_shot_followup_check(self, reason: str, is_voice: bool) -> None:
        """Per explicit instruction (2026-09-11): distinguishes "the
        operation never even started" (a genuine rejection -- no balance,
        or an in-flight rate-limit rejection -- see _schedule_api_retry) from
        "the operation genuinely completed" (a real AssistantMessage came
        through -- e.g. the CC CLI's own "you've hit your session limit"
        reply -- the turn is over, just concluded by reporting a cap). Both
        cases retry forever, flat interval, same as every other retry in
        this codebase (RESTART_BACKOFF_MS, MCP_RECONNECT_INTERVAL_MS,
        api_retry_timer) -- the only real difference is HOW each retry is
        delivered: _schedule_api_retry resubmits the same still-pending
        turn (nothing happened yet), this one injects a fresh follow-up
        check (the turn already completed once).

        Bug fix (2026-09-22), per explicit instruction, after a real
        incident: a 2026-09-14 change (since reverted -- see git history)
        made this give up permanently after exactly one retry per "cap
        episode" (one_shot_followup_used_for_limit), reasoning that
        _schedule_api_retry's own forever-retry had once fired for hours
        after a task was already genuinely finished. That reasoning doesn't
        apply here: this branch only ever fires when the model's own reply
        JUST reported a real, current usage cap -- there is no ambiguity to
        protect against, and confirmed live, giving up after one attempt
        left a real, still-pending task abandoned for 6+ hours after the
        cap had long since reset, with nothing to resume it. Every firing
        that ALSO concludes by reporting the same still-active cap goes
        through the SAME cc_cli_limit_message handler again, which calls
        this method again -- and now simply re-arms, exactly like every
        other flat retry in this codebase, until a real answer (anything
        other than another cap report) comes through."""
        if self._stopped_by_user("one_shot_followup", reason):
            return
        if self.one_shot_followup_timer:
            log_event("engine", "one_shot_followup_already_pending", tab_id=self.tab_id, reason=reason)
            return
        log_event(
            "engine", "one_shot_followup_scheduled", tab_id=self.tab_id, reason=reason,
            delay_ms=ONE_SHOT_FOLLOWUP_CHECK_DELAY_MS,
        )
        # Per explicit instruction (2026-10-12): same restart-survival fix as
        # _schedule_api_retry's own save_pending_api_retry -- see
        # durability.py's own "pending API retry" section.
        save_pending_one_shot_followup(self.workspace_dir, self.tab_id, reason, is_voice)

        def _fire() -> None:
            self.one_shot_followup_timer = None
            if self.ended:
                return
            log_event("engine", "one_shot_followup_firing", tab_id=self.tab_id, reason=reason)
            clear_pending_one_shot_followup(self.workspace_dir, self.tab_id)
            self.inject_proactive(
                f"[Internal: automatic follow-up check -- your previous turn concluded by reporting a usage cap, "
                f"and this check fires again every 90s until a real reply gets through. The user has not said "
                f"anything new and is not waiting on a repeated confirmation -- if you already told them about "
                f"finished work in an earlier reply and nothing has changed since, do NOT restate, re-confirm, "
                f"or apologize about it again, even briefly; reply with exactly [[NO_UPDATE]] instead.] "
                f"{CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=current_language_name(self.tab_id))}",
                is_voice,
            )
            # No rescheduling here -- if the cap is still active, the model's
            # own reply reports it again, which routes back through the
            # cc_cli_limit_message handler and calls this method fresh. That
            # IS the retry loop now, same shape as every other flat retry.

        loop = asyncio.get_event_loop()
        self.one_shot_followup_timer = loop.call_later(ONE_SHOT_FOLLOWUP_CHECK_DELAY_MS / 1000, _fire)
    # edition:end
    def _clear_one_shot_followup_timer(self, clear_persisted: bool = True) -> None:
        """Same `clear_persisted=False`-only-from-dispose() reasoning as
        _clear_api_retry_timer's own docstring."""
        if self.one_shot_followup_timer:
            self.one_shot_followup_timer.cancel()
            self.one_shot_followup_timer = None
        if clear_persisted:
            clear_pending_one_shot_followup(self.workspace_dir, self.tab_id)
    def _clear_mcp_reconnect_timers(self) -> None:
        for t in self.mcp_reconnect_timers.values():
            t.cancel()
        self.mcp_reconnect_timers.clear()
    # edition:cut claude client mcp reconnect
    def _schedule_mcp_reconnect(self, client: AgentEngine, name: str, attempt: int = 0) -> None:
        """Keeps retrying client.reconnect_mcp_server(name) on a FLAT
        interval until it succeeds -- so a server that's down for a
        moment (its target not up yet, a transient launch race) comes
        back on its own instead of staying unavailable for the rest of
        the session, and WITHOUT tearing down the whole session (a
        single unrelated server being down previously produced an
        infinite restart-then-fail loop -- see the 'init' handling below).
        Flat, not exponential, per the same standing instruction as the
        restart budget's own RESTART_BACKOFF_MS (never gives up, no
        ceiling, no growing delay) -- the original TS ported this as
        exponential-with-a-cap; deliberately not followed here.

        Bound to the specific `client` instance active when the failure
        was reported: if that's no longer self.client by the time a retry
        fires (session restarted for an unrelated reason, or ended), the
        retry loop for it is abandoned -- the new session's own init
        handling will report and schedule fresh retries if still down."""
        if name in self.mcp_reconnect_timers:
            return  # already retrying this one

        def _fire() -> None:
            self.mcp_reconnect_timers.pop(name, None)
            if self.ended or self.client is not client:
                return  # superseded by a full session restart
            asyncio.create_task(self._do_mcp_reconnect(client, name, attempt))

        loop = asyncio.get_event_loop()
        self.mcp_reconnect_timers[name] = loop.call_later(MCP_RECONNECT_INTERVAL_MS / 1000, _fire)
    async def _do_mcp_reconnect(self, client: AgentEngine, name: str, attempt: int) -> None:
        try:
            await client.reconnect_mcp_server(name)
            log_event("engine", "mcp_server_reconnected", tab_id=self.tab_id, server=name, attempt=attempt)
        except Exception as exc:
            log_event("engine", "mcp_server_reconnect_failed", tab_id=self.tab_id, server=name, attempt=attempt, retry_in_ms=MCP_RECONNECT_INTERVAL_MS, error=str(exc))
            self._schedule_mcp_reconnect(client, name, attempt + 1)
    # edition:end
    # edition:cut claude-session code no longer reached
    def _handle_balance_exhausted(self) -> str:
        """Own-Anthropic credits/quota ran out. Nothing to fall back to and
        no top-up we can drive (it's the user's own Anthropic billing) --
        just surface it and let _schedule_api_retry keep probing."""
        log_event("engine", "balance_exhausted", tab_id=self.tab_id)
        return BALANCE_EXHAUSTED_MESSAGE
    def _handle_rate_limit_rejected(self, source: str, info: dict[str, Any]) -> None:
        resets_at = info.get("resets_at")
        # Bug fix (2026-09-10): confirmed live this produced a bogus 1970
        # date ("Resets: 1970-01-21T..."). The SDK's own RateLimitInfo
        # docs just say "Unix timestamp" (ambiguous on paper), but
        # dividing by 1000 landed ~20 days after the epoch -- resets_at is
        # already in SECONDS (the standard meaning of "Unix timestamp",
        # and what datetime.fromtimestamp() itself expects), not
        # milliseconds. No division.
        reset_text = f" Resets: {datetime.fromtimestamp(resets_at).isoformat()}." if resets_at else ""
        type_text = f" ({info.get('rate_limit_type')})" if info.get("rate_limit_type") else ""
        text = f"Hit the Claude usage limit{type_text}.{reset_text} Retrying automatically."
        log_event("engine", "rate_limit_rejected", tab_id=self.tab_id, source=source)
        self._set_conn_state("limited", text)
        self._schedule_api_retry(f"rate_limit_rejected:{source}", self.turn_is_voice)
    # edition:end

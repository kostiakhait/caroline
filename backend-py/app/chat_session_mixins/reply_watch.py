from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header


class ReplyWatchMixin:
    async def _check_reply_expected(self, channel: str, args: dict[str, Any]) -> None:
        """Judges, via the server-side turn.reply.expected microagent,
        whether the message just sent on `channel` actually calls for a
        reply -- then, for email only, arms a tapering watch of that
        mailbox (see REPLY_WATCH_SCHEDULE). Every messenger channel
        already notifies the instant a reply arrives on its own (a live
        push connection for Telegram/Slack/Discord/Signal/WhatsApp, or
        Ratatosk's own 15-second global poll -- already far tighter than
        any schedule this could add), so for those the judgment still
        runs and is logged, but nothing is scheduled: there is nothing a
        poll could do that the channel doesn't already do faster.

        Fail-open on any error/timeout, same philosophy as every other
        microagent check here: an infrastructure hiccup just means this
        one send goes unwatched, never a stuck/stale watch."""
        sent_message = self._email_sent_message_text(args) if channel == "email" else str(args.get("text") or "").strip()
        if not sent_message:
            return
        from app.plugins.voice_api import check_reply_expected

        try:
            result = await check_reply_expected(self._narration_task(), channel, sent_message)
        except Exception as exc:
            log_event("engine", "check_reply_expected_failed", tab_id=self.tab_id, channel=channel, error=str(exc))
            return
        if result is None or not result["needs_reply"]:
            return
        if channel != "email":
            log_event("engine", "reply_expected_no_poll", tab_id=self.tab_id, channel=channel, reason=result["reason"])
            return
        await self._arm_reply_watch(args)
    @staticmethod
    def _email_sent_message_text(args: dict[str, Any]) -> str:
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("html") or args.get("text") or "").strip()
        return f"Subject: {subject}\n\n{body}" if subject else body
    async def _arm_reply_watch(self, args: dict[str, Any]) -> None:
        """Captures the mailbox's current newest UID as the baseline and
        starts the tapering tick schedule (REPLY_WATCH_SCHEDULE). Needs
        real mailbox credentials (address/password/imapHost) --
        email_send can also relay through the shared no_reply@ identity
        with none of these given, which has no inbox of its own to watch,
        so that case is skipped. A later qualifying send replaces
        whatever watch was already running, same as _memory_replay_timer."""
        if not (args.get("address") and args.get("password") and args.get("imapHost")):
            return
        from app.plugins.email_plugin import email_latest_uid

        try:
            baseline_uid = await email_latest_uid(args["address"], args["password"], args["imapHost"])
        except Exception as exc:
            log_event("engine", "reply_watch_arm_failed", tab_id=self.tab_id, error=str(exc))
            return
        self._cancel_reply_watch()
        self._reply_watch = {
            "address": args["address"], "password": args["password"], "imapHost": args["imapHost"],
            "baseline_uid": baseline_uid, "phase": 0, "tick": 0,
        }
        log_event("engine", "reply_watch_armed", tab_id=self.tab_id, address=args["address"], baseline_uid=baseline_uid)
        self._schedule_reply_watch_tick(REPLY_WATCH_SCHEDULE[0][0])
    def _cancel_reply_watch(self) -> None:
        self._reply_watch = None
        if self._reply_watch_timer:
            self._reply_watch_timer.cancel()
            self._reply_watch_timer = None
    def _schedule_reply_watch_tick(self, delay_s: float) -> None:
        """Flat interval per phase, forever until the schedule is spent --
        same re-arm-on-each-tick shape as _arm_memory_replay_timer."""
        def _fire() -> None:
            self._reply_watch_timer = None
            if not self.ended and self._reply_watch:
                asyncio.ensure_future(self._reply_watch_tick())

        self._reply_watch_timer = asyncio.get_event_loop().call_later(delay_s, _fire)
    async def _reply_watch_tick(self) -> None:
        """One tick: checks the mailbox's newest UID against the baseline
        captured when the watch was armed (UIDs are monotonically
        increasing per folder -- see email_latest_uid's own docstring). A
        higher UID (or any UID at all, if the mailbox was empty at
        arm-time) means a reply arrived: stop watching and tell the user
        right away. Otherwise, advance through REPLY_WATCH_SCHEDULE's
        phases; once every phase's ticks are spent with nothing new, the
        window ends silently, no nudge."""
        watch = self._reply_watch
        if not watch or self.ended:
            return
        from app.plugins.email_plugin import email_latest_uid

        try:
            latest_uid = await email_latest_uid(watch["address"], watch["password"], watch["imapHost"])
        except Exception as exc:
            log_event("engine", "reply_watch_tick_failed", tab_id=self.tab_id, error=str(exc))
            latest_uid = None
        baseline_uid = watch["baseline_uid"]
        is_new = latest_uid is not None and (baseline_uid is None or latest_uid > baseline_uid)
        if is_new:
            self._reply_watch = None
            log_event("engine", "reply_watch_reply_detected", tab_id=self.tab_id, address=watch["address"])
            self.inject_proactive(f"[В почтовом ящике {watch['address']} пришло новое письмо -- похоже на ответ, которого мы ждали.]")
            return
        watch["tick"] += 1
        interval_s, max_ticks = REPLY_WATCH_SCHEDULE[watch["phase"]]
        if watch["tick"] >= max_ticks:
            watch["phase"] += 1
            watch["tick"] = 0
            if watch["phase"] >= len(REPLY_WATCH_SCHEDULE):
                self._reply_watch = None
                log_event("engine", "reply_watch_window_expired", tab_id=self.tab_id, address=watch["address"])
                return
            interval_s = REPLY_WATCH_SCHEDULE[watch["phase"]][0]
        self._schedule_reply_watch_tick(interval_s)
    async def _check_refusal_and_maybe_retry(self) -> None:
        """Per explicit instruction (2026-10-12): a separate concern from
        _check_permission_guard (that one is about a missing user APPROVAL;
        this one is about the assistant itself voluntarily declining the
        user's actual task, on its own judgment -- a real incident the same
        day: declining to modify/deploy its own permission-control code
        unsupervised). Runs once, right after a real user turn finishes
        with real visible text (see the ResultMessage call site and
        _turn_last_visible_text's own docstring).

        On a confirmed decline, automatically retries the SAME task once
        through the SW-subscription small-model path (_start_small_model_
        turn) -- a one-off hand-off for this task only, per explicit
        instruction: no persisted chat_mode change, nothing reverted
        afterward because nothing was changed. The declined Claude reply
        the user already saw is left exactly as sent (not hidden) -- the
        small-model's own answer simply follows it as a normal new turn.

        Fail-open on any error/timeout/malformed response, same philosophy
        as every other microagent check here: an infrastructure hiccup must
        never be mistaken for a decline."""
        text = self._turn_last_visible_text
        self._turn_last_visible_text = None
        if not text:
            return
        task = (self.last_real_user_question or "").strip()
        if not task:
            return
        from app.plugins.voice_api import check_refusal_guard

        try:
            result = await check_refusal_guard(task, text)
        except Exception as exc:
            log_event("engine", "check_refusal_guard_failed", tab_id=self.tab_id, error=str(exc))
            return
        if result is None or not result["declined"]:
            return
        log_event("engine", "refusal_guard_retrying_via_sw", tab_id=self.tab_id, reason=result["reason"])
        self._start_small_model_turn(task, [], True, self.turn_is_voice)
    def _on_outbound_message_sent(self, channel: str, args: dict[str, Any], result: Any) -> None:
        """Per explicit instruction (2026-10-12): registered with
        session_context's register_reply_watch, called synchronously by
        app/operations.py's dispatch() right after any send-type tool call
        succeeds (email_send or any messenger `*_send_message`). Must
        return immediately -- the real judgment/watching happens in a
        detached task, same fire-and-forget shape as _check_permission_
        guard's own submit()-time call site."""
        asyncio.ensure_future(self._check_reply_expected(channel, args))

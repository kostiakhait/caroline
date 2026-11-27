from __future__ import annotations

import json
from pathlib import Path

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
        """One watch per (mailbox, person written to): the newest UID of that person's mail is the baseline, then the
        tapering tick schedule (REPLY_WATCH_SCHEDULE). A send names only the mailbox's address -- the saved credentials
        are filled in here (until 2026-11-27 such a send, that is nearly every one, was silently left unwatched, and the
        answer to a letter went unreported). A mailbox with nothing saved (the shared no_reply@ relay) has no inbox to
        watch."""
        from app import action_ledger

        address = str(args.get("address") or "").strip()
        if not address:
            return
        try:
            creds = await self._reply_watch_credentials(address, args)
        except Exception as exc:
            log_event("engine", "reply_watch_arm_failed", tab_id=self.tab_id, error=str(exc)[:200])
            return
        people = sorted(action_ledger._addresses(action_ledger._as_text(args.get("to") or "")) - {address.lower()})
        for person in people:
            try:
                baseline = await self._reply_watch_latest_uid(creds, person)
            except Exception as exc:
                log_event("engine", "reply_watch_arm_failed", tab_id=self.tab_id, error=str(exc)[:200])
                continue
            watch = {"address": address, "from": person, "baseline_uid": baseline, "phase": 0, "tick": 0}
            self._reply_watches()[f"{address}|{person}"] = watch
            self._save_reply_watches()
            log_event("engine", "reply_watch_armed", tab_id=self.tab_id, address=address, person=person, baseline_uid=baseline)
            self._schedule_reply_watch_tick(f"{address}|{person}", REPLY_WATCH_SCHEDULE[0][0])
    async def _reply_watch_credentials(self, address: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        from app.plugins import email_credentials

        given = {k: v for k, v in (args or {}).items() if k in email_credentials.FIELDS}
        return await email_credentials.fill({**given, "address": address})
    async def _reply_watch_latest_uid(self, creds: dict[str, Any], person: str) -> int | None:
        from app.plugins.email_plugin import email_latest_uid

        return await email_latest_uid(creds["address"], creds["password"], creds["imapHost"], query=person)
    def _reply_watches(self) -> dict[str, dict[str, Any]]:
        return self.__dict__.setdefault("_reply_watch_map", {})
    def _reply_watch_timers(self) -> dict[str, Any]:
        return self.__dict__.setdefault("_reply_watch_timer_map", {})
    def _reply_watch_file(self) -> Path:
        return Path(self.workspace_dir) / f"reply-watch-{_sanitize_tab_id(self.tab_id)}.json"
    def _save_reply_watches(self) -> None:
        """No password is kept here: each tick fills it in again from the saved credentials."""
        try:
            self._reply_watch_file().write_text(json.dumps(self._reply_watches(), ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log_event("engine", "reply_watch_save_failed", tab_id=self.tab_id, error=str(exc)[:200])
    def _restore_reply_watches(self) -> None:
        """Watches survive a restart of the app (a headless session keeps none)."""
        if self.headless or self._reply_watches():
            return
        try:
            kept = json.loads(self._reply_watch_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for key, watch in (kept if isinstance(kept, dict) else {}).items():
            self._reply_watches()[key] = watch
            self._schedule_reply_watch_tick(key, REPLY_WATCH_SCHEDULE[watch["phase"]][0])
    def _schedule_reply_watch_tick(self, key: str, delay_s: float) -> None:
        """Flat interval per phase, forever until the schedule is spent --
        same re-arm-on-each-tick shape as _arm_memory_replay_timer."""
        timers = self._reply_watch_timers()
        if key in timers:
            timers.pop(key).cancel()

        def _fire() -> None:
            timers.pop(key, None)
            if not self.ended and key in self._reply_watches():
                asyncio.ensure_future(self._reply_watch_tick(key))

        timers[key] = asyncio.get_event_loop().call_later(delay_s, _fire)
    async def _reply_watch_tick(self, key: str) -> None:
        """One tick: the newest UID of the person's mail against the baseline (UIDs only grow within a folder). A higher
        one means they wrote: the watch ends and the model reads the letter and tells the user. Otherwise on through
        REPLY_WATCH_SCHEDULE; once it is spent, the watch ends silently."""
        watch = self._reply_watches().get(key)
        if not watch or self.ended:
            return
        try:
            creds = await self._reply_watch_credentials(watch["address"])
            latest = await self._reply_watch_latest_uid(creds, watch["from"])
        except Exception as exc:
            log_event("engine", "reply_watch_tick_failed", tab_id=self.tab_id, error=str(exc)[:200])
            latest = None
        baseline = watch["baseline_uid"]
        if latest is not None and (baseline is None or latest > baseline):
            self._reply_watches().pop(key, None)
            self._save_reply_watches()
            log_event("engine", "reply_watch_reply_detected", tab_id=self.tab_id, address=watch["address"], uid=latest)
            self.inject_proactive(
                f"[A new letter that mentions {watch['from']} is in {watch['address']} (INBOX, uid {latest}), arrived after the "
                "letter you sent them -- most likely the awaited answer. Read it now with email_get_message and tell the user "
                f"who wrote and what it says, in {current_language_name(self.tab_id)}. Send nothing to anyone.]"
            )
            return
        watch["tick"] += 1
        interval_s, max_ticks = REPLY_WATCH_SCHEDULE[watch["phase"]]
        if watch["tick"] >= max_ticks:
            watch["phase"] += 1
            watch["tick"] = 0
            if watch["phase"] >= len(REPLY_WATCH_SCHEDULE):
                self._reply_watches().pop(key, None)
                self._save_reply_watches()
                log_event("engine", "reply_watch_window_expired", tab_id=self.tab_id, address=watch["address"])
                return
            interval_s = REPLY_WATCH_SCHEDULE[watch["phase"]][0]
        self._save_reply_watches()
        self._schedule_reply_watch_tick(key, interval_s)
    # edition:cut claude-session code no longer reached
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
    # edition:end
    def _on_outbound_message_sent(self, channel: str, args: dict[str, Any], result: Any) -> None:
        """Per explicit instruction (2026-10-12): registered with
        session_context's register_reply_watch, called synchronously by
        app/operations.py's dispatch() right after any send-type tool call
        succeeds (email_send or any messenger `*_send_message`). Must
        return immediately -- the real judgment/watching happens in a
        detached task, same fire-and-forget shape as _check_permission_
        guard's own submit()-time call site."""
        asyncio.ensure_future(self._check_reply_expected(channel, args))

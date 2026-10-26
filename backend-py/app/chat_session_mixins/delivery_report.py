from __future__ import annotations

import asyncio
import os

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header
from app import delivery_report

EARLIER_WINDOW_S = 30 * 60


class DeliveryReportMixin:
    """The end-of-turn check of what the user actually got (app/delivery_report.py). Hooks: _dr_begin (_input_stream,
    each real user message), _dr_observe (every wire message of the loop), the send wrapper installed by
    _dr_wrap_send (what really reached the window), _dr_finish (a real ResultMessage, after everything else has
    settled -- the ladder's verdict included). Nothing here can fail a turn or block the loop."""

    _dr: delivery_report.TurnRecord | None = None

    def _dr_wrap_send(self, send):
        async def recording_send(envelope):
            result = await send(envelope)
            try:
                if self._dr is not None:
                    self._dr.note_sent(envelope)
                self._dialogue_log_sent(envelope)
            except Exception:  # noqa: BLE001
                pass
            return result

        return recording_send

    def _dialogue_log_sent(self, envelope: Any) -> None:
        """What reached the window goes to the exact dialogue record (app/dialogue_log.py)."""
        if getattr(self, "headless", False):
            return
        text = delivery_report.delivered_text(envelope)
        if not text:
            return
        from app import dialogue_log

        kind = dialogue_log.REPORT if envelope.get("isDeliveryReport") else dialogue_log.SHOWN
        if text.startswith("[progress note] "):
            text, kind = text[len("[progress note] "):], dialogue_log.NOTE
        dialogue_log.append(self.workspace_dir, self.tab_id, dialogue_log.ASSISTANT, text, kind)

    def _dr_reset(self) -> None:
        self._dr = None

    def _dr_begin(self, item: dict[str, Any]) -> None:
        try:
            if item.get("ladder_continuation") or not item.get("memory_advice_for"):
                return  # an internal message, or the next rung of the turn in progress: not a new request
            text = item["memory_advice_for"]
            if self._dr is None:
                self._dr = delivery_report.TurnRecord(user_text=text)
            else:
                self._dr.user_text = (self._dr.user_text + "\n" + text).strip()
        except Exception as exc:  # noqa: BLE001
            self._dr = None
            log_event("engine", "delivery_report_begin_failed", tab_id=self.tab_id, error=str(exc)[:200])

    def _dr_observe(self, wire: dict[str, Any] | None) -> None:
        if self._dr is not None:
            try:
                self._dr.observe(wire)
            except Exception:  # noqa: BLE001
                pass

    def _dr_finish(self, message: Any) -> None:
        """A real turn has ended and its answer stands. Starts the check in the background."""
        record, self._dr = self._dr, None
        if record is None or os.environ.get("CAROLINE_NO_DELIVERY_CHECK") == "1":
            return
        if getattr(message, "is_error", False) or not record.has_work:
            return  # an error is handled by the retry machinery; with no work done there is nothing to report on
        asyncio.create_task(self._dr_check(record))

    def _dr_earlier_messages(self, record: delivery_report.TurnRecord) -> list[str]:
        """What the user has already been shown: this turn's messages and the recent ones from the exact dialogue record
        (reports included) -- a report must not say again what is there."""
        from app import dialogue_log

        texts = list(record.delivered)
        try:
            cutoff = (time.time() - EARLIER_WINDOW_S) * 1000
            logged = [e["text"] for e in dialogue_log.read_entries(self.workspace_dir, self.tab_id)
                      if e.get("role") == dialogue_log.ASSISTANT and e.get("kind") != dialogue_log.WITHHELD and e.get("ms", 0) >= cutoff]
            texts = logged[-10:] + [t for t in texts if t not in logged]
        except Exception:  # noqa: BLE001
            pass
        return texts

    async def _dr_check(self, record: delivery_report.TurnRecord) -> None:
        try:
            from app.small_model_engine import verify_delivery

            earlier = self._dr_earlier_messages(record)
            report = await verify_delivery(
                record.user_text, list(record.work.trace), record.delivered_transcript(), self.tab_id, "\n---\n".join(earlier[-6:]),
            )
            if not report:
                return
            if delivery_report.is_duplicate(report, earlier):
                log_event("engine", "delivery_report_skipped_duplicate", tab_id=self.tab_id, report=report[:300])
                return
            log_event("engine", "delivery_report_sent", tab_id=self.tab_id, delivered=len(record.delivered), report=report[:300])
            wire = {"type": "assistant", "message": {"content": [{"type": "text", "text": report}]}}
            await self.send({"type": "sdk_message", "message": wire, "isVoice": False, "isDeliveryReport": True})
        except Exception as exc:  # noqa: BLE001
            log_event("engine", "delivery_report_failed", tab_id=self.tab_id, error=str(exc)[:200])

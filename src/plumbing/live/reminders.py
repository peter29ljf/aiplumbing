"""Nudging a technician who has not answered a job offer yet.

A Telegram message can be missed — a phone in a pocket, a hand holding a wrench. A real
phone call cuts through and costs money every time; these cost nothing, so they are what
gets tried first. If jobs still slip through after this, that is *evidence* for buying the
calls rather than a guess.

Three things this is careful about:

**It stops the moment they answer.** The offer state is the single source of truth, so a
tap that lands between two ticks cancels the next nudge without any cross-talk.

**It gives up.** A bot that keeps asking is a bot that gets muted, and a muted bot misses
the next job too. After the last reminder the offer is flagged for the office and the
nudging stops.

**A crash in here must not take the server down.** It runs on its own thread and swallows
its own errors — the customer-facing endpoints have nothing to do with whether a reminder
went out.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

TICK_SECONDS = 30

# What each nudge says. Escalating, but never scolding: the technician is working, and a
# message that reads like nagging gets the whole chat muted.
NUDGE = [
    "Still waiting on this one when you get a moment.",
    "This job is still unanswered — the customer is expecting to hear back.",
    "Last reminder on this one. If I hear nothing I will flag it for the office.",
]
GAVE_UP = (
    "No answer on this job, so I have flagged it for the office. "
    "You can still accept it if you are free."
)


class ReminderLoop:
    """Checks every half minute for offers nobody has answered."""

    def __init__(self, offers: Any, rules: dict[str, Any], send: Any = None) -> None:
        self.offers = offers
        config = (rules or {}).get("job_offer", {}) or {}
        self.schedule: list[int] = list(config.get("reminder_minutes") or [5, 15, 30])
        self.give_up: bool = bool(config.get("give_up_after_last_reminder", True))
        self._send = send or _send_telegram
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad tick must not end the loop
                continue

    # ------------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """One pass. Returns what it sent, so a test does not need to wait 30 seconds."""
        now = now or datetime.now().astimezone()
        sent: list[dict[str, Any]] = []

        for offer in self._unanswered():
            age = now - _parse(offer["created_at"])
            due = self._due_count(age)
            already = int(offer["reminders_sent"] or 0)
            if due <= already:
                continue

            last = due >= len(self.schedule)
            if last and self.give_up:
                text = GAVE_UP
            else:
                text = NUDGE[min(due - 1, len(NUDGE) - 1)]

            self._send(offer["chat_id"], f"{text}\n\n{offer['summary']}")
            self._mark(offer["offer_id"], due, gave_up=last and self.give_up)
            sent.append({"offer_id": offer["offer_id"], "number": due, "gave_up": last and self.give_up})

        return sent

    def _due_count(self, age: timedelta) -> int:
        minutes = age.total_seconds() / 60
        return sum(1 for threshold in self.schedule if minutes >= threshold)

    def _unanswered(self) -> list[dict[str, Any]]:
        with self.offers.store.connect() as conn:
            rows = conn.execute(
                "SELECT offer_id, ticket_id, chat_id, summary, created_at, "
                "COALESCE(reminders_sent, 0) AS reminders_sent "
                "FROM offers WHERE state IN ('sent','awaiting_reason') AND nudging = 1"
            ).fetchall()
        return [dict(r) for r in rows]

    def _mark(self, offer_id: str, count: int, *, gave_up: bool) -> None:
        with self.offers.store.connect(write=True) as conn:
            conn.execute(
                "UPDATE offers SET reminders_sent = ?, nudging = ?, updated_at = ? "
                "WHERE offer_id = ?",
                (count, 0 if gave_up else 1, datetime.now().astimezone().isoformat(), offer_id),
            )
        if gave_up:
            # Filed against the ticket, not just the offer. The office looks things up by
            # the job the customer is waiting on, and an event with no ticket_id is an
            # event nobody will find.
            offer = self.offers.get(offer_id)
            self.offers.store.add_event(
                "job_offer_unanswered",
                ticket_id=offer.ticket_id if offer else "",
                detail=f"{count} reminders sent, no answer",
                offer_id=offer_id,
            )


def _send_telegram(chat_id: str, text: str) -> None:
    from plumbing.integrations import is_live  # noqa: PLC0415

    if not is_live("telegram.send"):
        return
    from plumbing.integrations import telegram  # noqa: PLC0415
    from plumbing.integrations.gate import LiveToolUnavailable  # noqa: PLC0415

    try:
        telegram.send_message(chat_id, text)
    except LiveToolUnavailable:
        return


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed

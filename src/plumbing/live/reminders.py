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

# The day-after question. Two answers, because those are the only two that change what
# the office does: the work happened, or the customer decided against it. Anything more
# detailed is a conversation, and the technician can just type it.
OUTCOME_PROMPT = "How did this one go?"
OUTCOME_DONE = "od"
OUTCOME_DECLINED = "oc"

# What the customer is told once the technician says the work is finished. The technician
# is not named: he confirmed it in a Telegram tap, not in a sentence to be quoted, and a
# name attached to a claim he did not phrase is a name he has to defend.
THANKS = (
    "Thanks for choosing {company}. The technician has confirmed the work is complete. "
    "If anything isn't right, reply to this message and we'll come back out."
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
            for step in (self.tick, self.tick_followups):
                try:
                    step()
                except Exception:  # noqa: BLE001 - a bad pass must not end the loop
                    continue

    # ------------------------------------------------------------------
    def tick_followups(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Ask the technician how a booked job went, once it is due.

        The agent schedules this during a conversation that then ends. Nothing about the
        conversation survives, so the question has to be driven from here — before this
        existed the follow-up was recorded in memory and fired exactly never.
        """
        now = now or datetime.now().astimezone()
        asked: list[dict[str, Any]] = []
        store = self.offers.store

        for followup in store.due_followups(now):
            if not followup["chat_id"]:
                continue
            if followup["status"] == "asked":
                # Already asked and still unanswered. One reminder, then leave it: the
                # customer is not waiting on this, so nagging costs more than it gains.
                if int(followup["asked_count"] or 0) >= 2:
                    store.update_followup(followup["followup_id"], status="given_up")
                    store.add_event(
                        "followup_unanswered", ticket_id=followup["ticket_id"],
                        detail="no outcome reported", followup_id=followup["followup_id"],
                    )
                    continue

            self._send(
                followup["chat_id"],
                f"{OUTCOME_PROMPT}\n\n{followup['summary']}",
                buttons=[[
                    {"text": "✅ Done", "data": f"f:{followup['followup_id']}:{OUTCOME_DONE}"},
                    {"text": "🚫 Customer declined",
                     "data": f"f:{followup['followup_id']}:{OUTCOME_DECLINED}"},
                ]],
            )
            store.update_followup(
                followup["followup_id"], status="asked",
                asked_count=int(followup["asked_count"] or 0) + 1,
            )
            asked.append({"followup_id": followup["followup_id"]})

        return asked

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


def close_out(store: Any, followup_id: str, *, done: bool, note: str = "") -> dict[str, Any]:
    """The technician has answered. Record it, and if the job happened, finish the ticket.

    This is the only place a ticket reaches `Closed`. Nothing in the conversation can do
    it: by the time the work is done the customer left hours ago and the agent that spoke
    to them is gone. Before this existed the answer was filed and the ticket stayed open
    forever — a record nobody looks at again, on a job that went perfectly well.

    The customer is thanked in the same breath, because being told the job is finished is
    the last thing they are owed and nobody else is going to say it. Never raises: a text
    that will not send must not stop the ticket closing.
    """
    row = _followup(store, followup_id)
    if row is None:
        return {"ok": False, "error": f"No follow-up '{followup_id}'"}

    store.update_followup(followup_id, status="answered",
                          answer=note or ("done" if done else "customer_declined"))
    store.add_event("job_outcome_reported", ticket_id=row["ticket_id"],
                    detail=note or ("done" if done else "customer_declined"),
                    followup_id=followup_id)
    if not done:
        # The customer decided against the work. The job is over either way, so the ticket
        # closes — but nobody is thanked for work that did not happen.
        _close(store, row["ticket_id"])
        return {"ok": True, "closed": True, "thanked": False}

    _close(store, row["ticket_id"])
    return {"ok": True, "closed": True, "thanked": _thank(store, row["ticket_id"])}


def _followup(store: Any, followup_id: str) -> dict[str, Any] | None:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM followups WHERE followup_id = ?", (followup_id,)
        ).fetchone()
    return dict(row) if row else None


def _close(store: Any, ticket_id: str) -> None:
    ticket = store.ticket(ticket_id) if ticket_id else None
    if ticket is None or ticket["status"] == "Closed":
        return
    history = list(ticket["history"]) + [f"{ticket['status']} -> Closed"]
    store.save_ticket({**ticket, "customer_phone": ticket["phone"],
                       "status": "Closed", "history": history})
    store.add_event("ticket_status_changed", ticket_id=ticket_id, detail="Closed")


def _thank(store: Any, ticket_id: str) -> bool:
    """The last message of the job. Returns whether it went."""
    from plumbing import config  # noqa: PLC0415

    ticket = store.ticket(ticket_id) if ticket_id else None
    if ticket is None:
        return False
    # The tag, then the column. `ticket.phone` is only set when a tool happened to pass
    # "phone"; the one reliably there is what the engine copied off the lookup.
    number = str(ticket["tags"].get("phone") or ticket["phone"] or "")
    if not number:
        return False

    body = THANKS.format(company=config.business_rules()["company"]["name"])
    store.add_message(channel="sms", speaker="agent", text=body,
                      phone=number, ticket_id=ticket_id)

    from plumbing.integrations import is_live  # noqa: PLC0415

    if not is_live("sms.send"):
        return False
    from plumbing.integrations import twilio_sms  # noqa: PLC0415
    from plumbing.integrations.gate import LiveToolUnavailable  # noqa: PLC0415

    try:
        twilio_sms.send_sms(number, body)
    except LiveToolUnavailable:
        return False
    return True


def _send_telegram(chat_id: str, text: str, buttons: Any = None) -> None:
    from plumbing.integrations import is_live  # noqa: PLC0415

    if not is_live("telegram.send"):
        return
    from plumbing.integrations import telegram  # noqa: PLC0415
    from plumbing.integrations.gate import LiveToolUnavailable  # noqa: PLC0415

    try:
        telegram.send_message(chat_id, text, buttons=buttons)
    except LiveToolUnavailable:
        return


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed

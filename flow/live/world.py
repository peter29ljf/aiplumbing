"""The real world: a database that outlives the process, and services that reach people.

The other half of `flow/sim/world.py`. Same surface — the list of it is in `flow/world.py`
— so the tools in `flow/sim/tools.py` run against either without knowing which, and the
node prompts that were tested against the simulator are the ones a customer talks to.

**Nothing here decides whether a message really goes out.** Every outbound leg asks
`is_live(...)` first, which reads the environment on the production machine and answers no
everywhere else. So this module is safe to run on a laptop: it will write to sqlite and
record what it *would* have sent, and no text, email or calendar entry leaves the process
until somebody sets the switches on the server. See `plumbing/integrations/gate.py`.

**A failed send raises.** `Refused` goes back to the model, which is the whole point:
`always.md` forbids describing an action it has not taken, and the engine will not let a
last step sign off with its own tools uncalled. So a Telegram outage stops `booking`
telling a customer somebody is coming — which is the honest outcome, and the one the old
system had to learn the hard way when a fictional phone number swallowed dispatches in
silence.

**The in-memory lists are still here.** `texts`, `emails`, `escalations` and the rest are
kept alongside the real writes, so `snapshot()` still answers the same question and the
console can show what one conversation actually did. They are a record of this process's
work, not the source of truth; the database is.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from flow.runner.memory import NODE_TAG  # noqa: E402
from flow.world import (  # noqa: E402
    Appointment,
    Customer,
    Job,
    Refused,
    Technician,
    Ticket,
    phone_key,
)
from plumbing import config  # noqa: E402
from plumbing.integrations import LiveToolUnavailable, is_live  # noqa: E402

# How far ahead a slot search looks, and how many it offers. Three, because a customer
# choosing from three times chooses; a customer choosing from ten reads a timetable.
SLOT_DAYS = 7
SLOT_LIMIT = 3

# Which switch each tool answers to, and it is not always its own name.
#
# A gate key is a *service*, not a tool: `technician.notify` and `escalate.raise` both
# reach a person through Telegram and both stop the moment Telegram is off, so both are
# `telegram.send`. Reading a tool's own name instead is a mistake that looks harmless and
# is not — the console reported `technician.notify` as simulated while it was quite
# happily messaging a real technician, which is exactly the kind of thing somebody reads
# once and then stops checking.
#
# Tools not named here never leave the process. They read and write the database, which is
# this deployment's own record and not a switch anybody flips.
GATES = {
    "sms.send": "sms.send",
    "email.send": "email.send",
    "technician.notify": "telegram.send",
    "escalate.raise": "telegram.send",
    "calendar.find_slots": "calendar.find_slots",
    "calendar.create_appointment": "calendar.create_appointment",
    # Not `calendar.find_booking`: an appointment we made is a row in our own database,
    # and finding it needs nobody's permission.
}


class LiveWorld:
    """One conversation, over durable state.

    Cheap to build — it holds no connection and reads nothing until asked — so one per
    conversation is fine, and each is a plain object with no cross-talk between them.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.rules = config.business_rules()
        self.tz = ZoneInfo(self.rules["company"]["timezone"])

        self.technicians: dict[str, Technician] = _roster()

        # Tickets this conversation has touched, kept as objects so a node can write to
        # `tags` the way it does in the simulator. Every write goes through to the
        # database; this is a working set, not a cache to be trusted over the rows.
        self.tickets: dict[str, Ticket] = {}
        self.appointments: dict[str, Appointment] = {}

        # What this conversation did. The durable record is in the database and in the
        # providers' own logs; this is what `snapshot()` reports and what the console
        # shows for one exchange.
        self.texts: list[dict[str, Any]] = []
        self.emails: list[dict[str, Any]] = []
        self.technician_messages: list[dict[str, Any]] = []
        self.escalations: list[dict[str, Any]] = []
        self.followups: list[dict[str, Any]] = []

        self.ended = False
        self.end_reason = ""

    # ---- when --------------------------------------------------------
    @property
    def now(self) -> datetime:
        """The real clock, in the business's timezone.

        A property rather than a field. The simulator freezes time because a scenario has
        to be repeatable; a conversation with a person lasts minutes, and a frozen clock
        would offer somebody at 17:58 a slot that closed while they were typing.
        """
        return datetime.now(self.tz)

    # ---- identity ----------------------------------------------------
    def find_customer(self, phone: str) -> Customer | None:
        row = self.store.find_customer(phone)
        if row is None:
            return None
        return Customer(
            phone=row["phone"],
            name=row.get("name", ""),
            address=row.get("address", ""),
            email=row.get("email", ""),
            property_type=row.get("property_type", ""),
            jobs=[
                Job(
                    job_id=job["job_id"],
                    what=job.get("service_name") or job.get("service_type", ""),
                    finished_on=(job.get("completed_at") or "")[:10],
                    technician=self._technician_name(job.get("technician_id") or ""),
                )
                for job in row.get("jobs", [])
            ],
            open_work=self._open_work(row["phone"]),
        )

    def _open_work(self, phone: str) -> list[dict[str, Any]]:
        """Their tickets that nobody has closed, newest first.

        The summary a step gets is about *this* conversation, so without this a customer
        who rang last week and rang again is met as a stranger with a familiar name. Kept
        short on purpose: what it was about and where it got to, not the words.
        """
        found = []
        for ticket in self.store.open_tickets(phone)[:3]:
            tags = ticket.get("tags") or {}
            found.append({
                "ticket_id": ticket["ticket_id"],
                "status": ticket["status"],
                "about": tags.get("issue") or "",
                "last_touched": (ticket.get("updated_at") or "")[:10],
            })
        return found

    def add_customer(self, phone: str, **fields: Any) -> Customer:
        """Create or update. The store merges rather than overwrites, so a conversation
        that learns only a name cannot wipe an address collected last week."""
        jobs = fields.pop("jobs", [])
        self.store.upsert_customer(phone, **fields)
        for job in jobs:
            self.store.add_job(phone, job)
        found = self.find_customer(phone)
        if found is None:  # pragma: no cover - the row was written a line ago
            raise Refused(f"The record for {phone} could not be read back after saving.")
        return found

    # ---- the ticket, which is also the memory -------------------------
    def open_ticket(self, phone: str = "") -> Ticket:
        ticket = Ticket(id=self.store.next_id("TK"), phone=phone)
        self.tickets[ticket.id] = ticket
        self._save(ticket)
        return ticket

    def ticket(self, ticket_id: str) -> Ticket:
        """The ticket, from this conversation or from the database.

        Reading it back matters: a customer who texts today about a job booked last week
        is on a new conversation with an empty working set, and the ticket they are
        talking about is a row nobody has loaded.
        """
        found = self.tickets.get(ticket_id)
        if found is not None:
            return found

        row = self.store.ticket(ticket_id)
        if row is None:
            raise Refused(
                f"No ticket '{ticket_id}'. Open ones: {sorted(self.tickets)}"
            )
        found = Ticket(id=row["ticket_id"], status=row["status"], phone=row["phone"],
                       tags=row["tags"], history=row["history"])
        self.tickets[found.id] = found
        return found

    def set_status(self, ticket_id: str, status: str) -> None:
        ticket = self.tickets.get(ticket_id)
        if ticket is None or ticket.status == status:
            return
        ticket.history.append(f"{ticket.status} -> {status}")
        ticket.status = status
        self._save(ticket)
        self.store.add_event("ticket_status_changed", ticket_id=ticket.id, detail=status)

    def remember(self, ticket_id: str, fields: dict[str, Any]) -> Ticket:
        """Put facts on the ticket and write the row.

        Every tag mutation goes through here — the `ticket.set_fields` tool, the engine
        copying a tool's `remembers`, and the engine noting which node it is on. Letting
        any of them reach into `ticket.tags` directly would work perfectly in the simulator
        and lose the write here, and the fault would look like the model forgetting things
        it had just been told.
        """
        ticket = self.ticket(ticket_id)
        ticket.tags.update(fields)
        if fields.get("phone") and not ticket.phone:
            ticket.phone = str(fields["phone"])
        self._save(ticket)
        return ticket

    # ---- the diary ---------------------------------------------------
    def free_slots(self, *, days: int = SLOT_DAYS, limit: int = SLOT_LIMIT) -> list[datetime]:
        """The next few working hours nobody has taken.

        Three sources of "taken", and all three matter: our own bookings in the database,
        anything a person put in the real calendar by hand, and the hours the business is
        simply shut. Missing the second is how a technician gets sent to two places at
        once, because the one he wrote in himself is invisible from here.
        """
        schedule = self.rules["schedule"]
        opens = int(str(schedule["working_hours"]["start"]).split(":")[0])
        closes = int(str(schedule["working_hours"]["end"]).split(":")[0])
        holidays = {str(d) for d in schedule.get("public_holidays", [])}

        now = self.now
        stop = now + timedelta(days=days)
        busy = self._busy_between(now, stop)

        found: list[datetime] = []
        when = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        while when < stop and len(found) < limit:
            closed = when.weekday() == 6 or when.date().isoformat() in holidays
            if not closed and opens <= when.hour < closes and not _taken(when, busy):
                found.append(when)
            when += timedelta(hours=1)
        return found

    def _busy_between(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        busy = [
            (datetime.fromisoformat(row["start_at"]),
             datetime.fromisoformat(row["start_at"])
             + timedelta(minutes=int(row["duration_minutes"] or 0)))
            for row in self.store.appointments_between(start, end)
        ]
        if not is_live("calendar.find_slots"):
            return busy

        from plumbing.integrations import google_calendar  # noqa: PLC0415

        try:
            busy.extend(google_calendar.busy_periods(start, end))
        except LiveToolUnavailable as exc:
            # Refused rather than quietly offering our own view of the diary. Times we
            # believe are free and the technician knows are not is worse than telling the
            # customer we cannot check right now — the first books somebody into a slot he
            # will not turn up for.
            raise Refused(
                f"The calendar could not be read, so there is no way to know what is free: "
                f"{exc}. Do not offer any times. Take what they need and hand it to a "
                f"person with escalate.raise if this step has one."
            ) from exc
        return busy

    def book(self, **fields: Any) -> Appointment:
        appointment = Appointment(id=self.store.next_id("AP"), **fields)
        self.appointments[appointment.id] = appointment

        event_id = ""
        if is_live("calendar.create_appointment"):
            from plumbing.integrations import google_calendar  # noqa: PLC0415

            technician = self.technicians.get(appointment.technician)
            try:
                event_id = google_calendar.create_event(
                    start=appointment.starts,
                    duration_minutes=appointment.minutes,
                    summary=f"{appointment.what} — {appointment.address}",
                    description=(
                        f"Ticket {appointment.ticket_id}\n"
                        f"Customer: {appointment.phone}\n"
                        f"Technician: {technician.name if technician else ''}"
                    ),
                    location=appointment.address,
                )
            except LiveToolUnavailable as exc:
                # Nothing is written and nothing is confirmed. A booking that exists only
                # in our database is a booking the technician never sees.
                raise Refused(
                    f"The appointment could not be put in the diary: {exc}. It is not "
                    f"booked. Do not tell them it is."
                ) from exc

        self.store.save_appointment(
            {
                "appointment_id": appointment.id,
                "kind": "standard",
                "ticket_id": appointment.ticket_id,
                "customer_phone": appointment.phone,
                "technician_id": appointment.technician,
                "start": appointment.starts,
                "duration_minutes": appointment.minutes,
                "address": appointment.address,
                "description": appointment.what,
                "status": "booked",
            },
            calendar_event_id=event_id,
        )
        self.store.add_event("appointment_booked", ticket_id=appointment.ticket_id,
                             detail=appointment.starts.isoformat(),
                             appointment_id=appointment.id, calendar_event_id=event_id)
        return appointment

    def find_appointments(self, phone: str) -> list[Appointment]:
        """Theirs, soonest first, from now on. Matched on the number, not the ticket.

        Somebody ringing to move a visit booked last week is on a new ticket, so looking
        by ticket finds nothing and the conversation tells them they have no appointment —
        which is worse than not looking at all.
        """
        key = phone_key(phone)
        if not key:
            return []
        window_start = self.now - timedelta(hours=12)
        rows = self.store.appointments_between(window_start, self.now + timedelta(days=365))
        found = [
            Appointment(
                id=row["appointment_id"],
                ticket_id=row["ticket_id"],
                starts=datetime.fromisoformat(row["start_at"]),
                minutes=int(row["duration_minutes"] or 0),
                technician=row["technician_id"] or "",
                address=row["address"] or "",
                what=row["description"] or "",
                phone=row["phone"] or "",
            )
            for row in rows
            if phone_key(row["phone"] or "") == key
        ]
        for appointment in found:
            self.appointments.setdefault(appointment.id, appointment)
        return sorted(found, key=lambda a: a.starts)

    # ---- telling people ----------------------------------------------
    def send_sms(self, to: str, body: str) -> dict[str, Any]:
        record = {"to": to, "body": body, "at": self.now.isoformat(), "live": False}

        if is_live("sms.send"):
            from plumbing.integrations import twilio_sms  # noqa: PLC0415

            try:
                sent = twilio_sms.send_sms(to, body)
            except LiveToolUnavailable as exc:
                raise Refused(
                    f"The text could not be sent to {to}: {exc}. They have not been told."
                ) from exc
            record["live"] = True
            record["provider_message_id"] = sent["message_id"]

        self.texts.append(record)
        self.store.add_message(channel="sms", speaker="agent", text=body, phone=to)
        return {"sent": True, "to": to, "live": record["live"]}

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        record = {"to": to, "subject": subject, "body": body,
                  "at": self.now.isoformat(), "live": False}

        if is_live("email.send"):
            from plumbing.integrations import gmail_email  # noqa: PLC0415

            try:
                sent = gmail_email.send_email(to, subject, body)
            except LiveToolUnavailable as exc:
                raise Refused(
                    f"The email could not be sent to {to}: {exc}. Nothing has reached "
                    f"them, so do not say it is on its way."
                ) from exc
            record["live"] = True
            record["provider_message_id"] = sent["message_id"]

        self.emails.append(record)
        self.store.add_event("email_sent", detail=subject, to=to, live=record["live"])
        return {"sent": True, "to": to, "live": record["live"]}

    def notify_technician(self, technician_id: str, subject: str,
                          body: str) -> dict[str, Any]:
        """The job, in front of the technician, with Accept and Decline under it.

        The buttons are why this goes through `plumbing.live.notify` rather than sending a
        plain message: a technician who cannot answer in one tap is a technician the office
        has to chase, and Decline asks for a reason so whoever picks it up is not guessing
        at what to tell the customer.
        """
        technician = self.technicians.get(technician_id)
        if technician is None:
            raise Refused(
                f"No technician '{technician_id}'. On duty: {sorted(self.technicians)}"
            )

        record = {"technician_id": technician_id, "subject": subject, "body": body,
                  "channel": "telegram", "at": self.now.isoformat(), "live": False}

        if is_live("telegram.send"):
            if not technician.telegram:
                # Refused rather than dropped. Somebody unreachable is somebody who is not
                # coming, and the customer must not be told otherwise.
                raise Refused(
                    f"{technician.name} has no Telegram, so there is no way to reach them "
                    f"and nobody has been told about this job. Do not confirm it. Use "
                    f"escalate.raise so a person sorts it out."
                )
            from plumbing.live import notify  # noqa: PLC0415
            from plumbing.live.offers import Offers  # noqa: PLC0415

            outcome = notify.offer_job(
                offers=Offers(self.store),
                ticket_id=self._only_ticket(),
                chat_id=technician.telegram,
                phone=technician.phone,
                summary=f"{subject}\n\n{body}",
            )
            if outcome["errors"]:
                raise Refused(
                    f"{technician.name} could not be reached: {'; '.join(outcome['errors'])}. "
                    f"Nobody has been told about this job, so do not confirm it."
                )
            record["live"] = True
            record["offer_id"] = outcome["offer_id"]

        self.technician_messages.append(record)
        return {"sent": True, "to": technician.name, "channel": "telegram",
                "live": record["live"]}

    def escalate(self, ticket_id: str, reason: str, details: str) -> dict[str, Any]:
        """Raised, and put in front of somebody. See the simulator's note on why both."""
        ticket = self.ticket(ticket_id)
        self.escalations.append({
            "ticket_id": ticket.id, "reason": reason, "details": details,
            "tags": dict(ticket.tags), "at": self.now.isoformat(),
        })
        self.store.add_event("escalated", ticket_id=ticket.id, detail=reason,
                             details=details, tags=dict(ticket.tags))

        technician = self.on_duty()
        if technician is not None:
            self.notify_technician(
                technician.id,
                f"{reason} — {ticket.id}",
                f"{details}\n\n{_readable(ticket.tags)}",
            )
        return {"raised": True, "ticket_id": ticket.id}

    def schedule_followup(self, ticket_id: str, hours: int) -> dict[str, Any]:
        """Owe the technician a question tomorrow.

        Written to the database rather than kept here, because the conversation that
        arranged it is about to end. `plumbing.live.reminders.ReminderLoop` is what
        actually asks; before that table existed a follow-up fired exactly never.
        """
        ticket = self.ticket(ticket_id)
        due = self.now + timedelta(hours=int(hours))
        technician = self.on_duty()

        followup_id = self.store.schedule_followup(
            ticket_id=ticket.id,
            kind="job_outcome",
            due_at=due,
            chat_id=technician.telegram if technician else "",
            summary=f"{ticket.id} — {ticket.tags.get('issue') or 'the job'}",
        )
        self.followups.append({"ticket_id": ticket.id, "followup_id": followup_id,
                               "due": due.isoformat(), "answered": False, "asked": 0})
        return {"scheduled": True, "due": due.isoformat()}

    # ---- what one conversation did -----------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "tickets": {t.id: {"status": t.status, "tags": t.tags, "history": t.history}
                        for t in self.tickets.values()},
            "appointments": [vars(a) | {"starts": a.starts.isoformat()}
                             for a in self.appointments.values()],
            "texts": self.texts,
            "emails": self.emails,
            "technician_messages": self.technician_messages,
            "escalations": self.escalations,
            "followups": self.followups,
            "ended": self.ended,
            "end_reason": self.end_reason,
        }

    # ------------------------------------------------------------------
    def _save(self, ticket: Ticket) -> None:
        self.store.save_ticket({
            "ticket_id": ticket.id,
            "customer_phone": ticket.phone,
            "status": ticket.status,
            "owner_agent": str(ticket.tags.get(NODE_TAG) or ""),
            "tags": ticket.tags,
            "history": ticket.history,
        })

    def on_duty(self) -> Technician | None:
        return next(iter(self.technicians.values()), None)

    def _only_ticket(self) -> str:
        """The ticket this conversation is on, for filing an offer against.

        There is exactly one — the engine opens it before the first word — so the first is
        the right one. Written as its own method because that is an assumption, and an
        assumption worth being able to find later.
        """
        return next(iter(self.tickets), "")

    def _technician_name(self, technician_id: str) -> str:
        """Whoever this is, on the roster or off it.

        Past jobs name the technician who did them, and that may be somebody who has since
        left the rota. Falling back to `self.technicians` alone would show a customer a
        raw id like `t_li` where their old invoice says David.
        """
        technician = self.technicians.get(technician_id) or _everyone().get(technician_id)
        return technician.name if technician else technician_id


def _everyone() -> dict[str, Technician]:
    """The whole roster from config/world_seed.yaml, on duty or not, reachable or not.

    Only for putting a name to an id — a job finished last year, a booking made before
    somebody's rota changed. Never for deciding who to send work to.
    """
    people: dict[str, Technician] = {}
    for spec in config.world_seed().get("technicians", []):
        people[spec["id"]] = Technician(
            id=spec["id"],
            name=spec.get("name", spec["id"]),
            telegram=str(spec.get("telegram_chat_id") or ""),
            phone=str(spec.get("phone") or ""),
            skills=tuple(spec.get("skills") or ()),
        )
    return people


def _roster() -> dict[str, Technician]:
    """Who work can actually be given to: on duty, and reachable.

    Both halves matter and for the same reason. Somebody not working will not go, and
    somebody with no Telegram cannot be told — Telegram is the only way the office reaches
    a technician (`plumbing/live/notify.py`). Either way the message goes nowhere and the
    customer has been told somebody is coming.

    Filtering here rather than refusing later is the difference between a booking that
    cannot be made and one that fails half way through, after the diary entry exists. The
    model is only ever shown people it can really send.
    """
    on_duty = {
        str(spec["id"]) for spec in config.world_seed().get("technicians", [])
        if spec.get("on_duty", True)
    }
    return {
        person.id: person for person in _everyone().values()
        if person.id in on_duty and person.telegram
    }


def _taken(when: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    finish = when + timedelta(hours=1)
    return any(when < end and finish > start for start, end in busy)


def _readable(tags: dict[str, Any]) -> str:
    return "\n".join(f"{k.replace('_', ' ').capitalize()}: {v}"
                     for k, v in tags.items() if v not in (None, "", [], {}))

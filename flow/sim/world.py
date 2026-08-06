"""The world the flow runs against. Everything in memory, nothing leaves the process.

Small on purpose. It holds what the sixteen tools in flow.yaml need and nothing else —
the old simulator carries fifty-one tools' worth of state, and copying it would have
brought the assumptions behind all of them along too.

**There are no gates here.** Booking a job somebody cannot be told about, walking a ticket
into a status that makes no sense, taking money before it is owed: all of it goes through.
That is deliberate. Every gate in the old system was added after a real failure, and this
rewrite is finding out which of those failures the new shape still has. A gate added
before the failure is a guess about what the model will get wrong, and guesses about that
have been wrong here before.

Money and thresholds come from `config/business_rules.yaml`, the same file the old system
reads. Inventing a second set of prices to test against would mean testing against prices
nobody charges.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from plumbing import config  # noqa: E402


def phone_key(number: str) -> str:
    """Last ten digits. `+1 (604) 721-8629` and `6047218629` are one customer."""
    return re.sub(r"\D", "", number or "")[-10:]


@dataclass
class Job:
    job_id: str
    what: str
    finished_on: str
    technician: str


@dataclass
class Customer:
    phone: str
    name: str = ""
    address: str = ""
    email: str = ""
    property_type: str = ""
    jobs: list[Job] = field(default_factory=list)


@dataclass
class Technician:
    id: str
    name: str
    telegram: str = ""
    skills: tuple[str, ...] = ()


@dataclass
class Appointment:
    id: str
    ticket_id: str
    starts: datetime
    minutes: int
    technician: str
    address: str
    what: str
    # Whose it is. The ticket knows, but a customer ringing about an appointment made last
    # week is on a new ticket, and the only thing they can be found by is their number.
    phone: str = ""


@dataclass
class Ticket:
    id: str
    status: str = "New Inquiry"
    phone: str = ""
    # Everything the conversation has concluded so far. This is the memory that survives
    # between nodes: each one reads it instead of the transcript, and writes its own
    # conclusion back. See flow/runner/memory.py.
    tags: dict[str, Any] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)


class World:
    """One conversation's worth of everything."""

    def __init__(self, now: str, seed: dict[str, Any] | None = None) -> None:
        self.rules = config.business_rules()
        self.tz = ZoneInfo(self.rules["company"]["timezone"])
        self.now = datetime.fromisoformat(now)

        seed = seed or {}
        self.customers: dict[str, Customer] = {}
        for spec in seed.get("customers", []):
            self.add_customer(**spec)

        self.technicians: dict[str, Technician] = {}
        for spec in seed.get("technicians") or [
            {"id": "t_wang", "name": "Mike Wang", "telegram": "6043701711"}
        ]:
            self.technicians[spec["id"]] = Technician(**spec)

        # Times somebody else already has. The flow only ever reads free slots around them.
        self.busy: list[tuple[datetime, datetime]] = [
            (datetime.fromisoformat(s), datetime.fromisoformat(e))
            for s, e in seed.get("busy", [])
        ]

        self.tickets: dict[str, Ticket] = {}
        self.appointments: dict[str, Appointment] = {}

        # What went out. Assertions read these; nothing else does.
        self.texts: list[dict[str, Any]] = []
        self.technician_messages: list[dict[str, Any]] = []
        self.escalations: list[dict[str, Any]] = []
        self.followups: list[dict[str, Any]] = []

        self.ended = False
        self.end_reason = ""
        self._counters: dict[str, int] = {}

        # Visits already in the diary before this conversation started. Somebody ringing to
        # move an appointment has to have had one, and there is no earlier conversation
        # here to have made it. Last, because booking one needs the counters.
        for spec in seed.get("appointments", []):
            spec = dict(spec)
            self.book(starts=datetime.fromisoformat(spec.pop("starts")),
                      minutes=int(spec.pop("minutes", 120)),
                      ticket_id=spec.pop("ticket_id", ""), **spec)

    # ---- identity ----------------------------------------------------
    def add_customer(self, phone: str, **fields: Any) -> Customer:
        jobs = [Job(**j) for j in fields.pop("jobs", [])]
        customer = Customer(phone=phone, jobs=jobs, **fields)
        self.customers[phone_key(phone)] = customer
        return customer

    def find_customer(self, phone: str) -> Customer | None:
        return self.customers.get(phone_key(phone))

    # ---- bookkeeping -------------------------------------------------
    def next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    def open_ticket(self, phone: str = "") -> Ticket:
        ticket = Ticket(id=self.next_id("TK"), phone=phone)
        self.tickets[ticket.id] = ticket
        return ticket

    def set_status(self, ticket_id: str, status: str) -> None:
        """Recorded, never refused. Which transitions are legal is a question this
        rewrite is asking, not answering — the sequence is printed after a run and read
        by a person."""
        ticket = self.tickets.get(ticket_id)
        if ticket is None or ticket.status == status:
            return
        ticket.history.append(f"{ticket.status} -> {status}")
        ticket.status = status

    # ---- the diary ---------------------------------------------------
    def free_slots(self, *, days: int = 7, limit: int = 3) -> list[datetime]:
        """The next few working hours nobody has taken.

        Hours and closed days come from the rules file. Sundays and statutory holidays are
        skipped because the business is shut, not because anything here is enforcing it.
        """
        schedule = self.rules["schedule"]
        opens = int(str(schedule["working_hours"]["start"]).split(":")[0])
        closes = int(str(schedule["working_hours"]["end"]).split(":")[0])
        holidays = {str(d) for d in schedule.get("public_holidays", [])}

        found: list[datetime] = []
        when = (self.now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        stop = self.now + timedelta(days=days)
        while when < stop and len(found) < limit:
            closed = when.weekday() == 6 or when.date().isoformat() in holidays
            if not closed and opens <= when.hour < closes and not self._taken(when):
                found.append(when)
            when += timedelta(hours=1)
        return found

    def _taken(self, when: datetime) -> bool:
        finish = when + timedelta(hours=1)
        booked = [(a.starts, a.starts + timedelta(minutes=a.minutes))
                  for a in self.appointments.values()]
        return any(when < end and finish > start for start, end in self.busy + booked)

    def book(self, **fields: Any) -> Appointment:
        appointment = Appointment(id=self.next_id("AP"), **fields)
        self.appointments[appointment.id] = appointment
        return appointment

    def find_appointments(self, phone: str) -> list[Appointment]:
        """Theirs, soonest first. Matched on the number, not the ticket.

        A customer ringing about a visit booked last week is on a new ticket, so looking
        by ticket would find nothing and the conversation would tell them they have no
        appointment — which is worse than not looking at all.
        """
        key = phone_key(phone)
        found = [a for a in self.appointments.values() if phone_key(a.phone) == key]
        return sorted(found, key=lambda a: a.starts)

    # ---- what a run is judged on -------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "tickets": {t.id: {"status": t.status, "tags": t.tags, "history": t.history}
                        for t in self.tickets.values()},
            "appointments": [vars(a) | {"starts": a.starts.isoformat()}
                             for a in self.appointments.values()],
            "texts": self.texts,
            "technician_messages": self.technician_messages,
            "escalations": self.escalations,
            "followups": self.followups,
            "ended": self.ended,
            "end_reason": self.end_reason,
        }

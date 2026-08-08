"""The simulated world. Everything in memory, nothing leaves the process.

One of two backends behind the same set of tools; the other is `flow/live/world.py`, which
writes to the database and calls real services. What both of them have to provide, and the
words they share, are in `flow/world.py`.

Small on purpose. It holds what the tools in flow.yaml need and nothing else —
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

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from bat.runtime.world import (  # noqa: E402,F401 - re-exported; callers import them from here
    Appointment,
    Customer,
    Job,
    Refused,
    Technician,
    Ticket,
    phone_key,
)


class World:
    """One conversation's worth of everything."""

    def __init__(self, now: str, seed: dict[str, Any] | None = None, *,
                 rules: dict[str, Any] | None = None,
                 records: tuple[str, ...] = ()) -> None:
        """`rules` is the project's business_rules.yaml, already read.

        It used to be fetched from a module that knew one company's config directory,
        which is most of how a general simulator came to be a plumbing company. Handed in,
        two projects can run in one process and neither can read the other's prices.
        """
        self.rules = rules or {}
        self.tz = ZoneInfo(self.rules["company"]["timezone"])
        self.now = datetime.fromisoformat(now)

        seed = seed or {}
        self.customers: dict[str, Customer] = {}
        for spec in seed.get("customers", []):
            self.add_customer(**spec)

        # What a project's own tools record, under names the kit does not know: a travel
        # agency sends `enquiries`, a takeaway takes `orders`. They reach the snapshot
        # alongside the built-in lists, so a scenario can assert on the thing the business
        # actually does rather than only on the six nouns a plumber has.
        # Seeded with the kinds this project's tools are capable of recording, so a
        # scenario asserting none of something is checking a real zero rather than an
        # absent key. `Project.records()` reads them off the tool source.
        self.extras: dict[str, list[Any]] = {kind: [] for kind in records}
        # What has already been done to the outside world, by tool and arguments, so
        # doing it again does nothing. See `registry.tool(once=True)`.
        self.done: dict[str, Any] = {}
        self.repeats: list[dict[str, Any]] = []

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
        self.emails: list[dict[str, Any]] = []
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

    # ---- telling people ----------------------------------------------
    #
    # Methods rather than the tools appending to these lists themselves. Appending to
    # `world.texts` is something a tool can do; sending a text is not, and the live world
    # has to reach Twilio to do it. Keeping the shape of the call the same on both sides
    # is what lets one set of tools drive either.
    #
    # Each one records what a run is judged on and returns what the model is told.

    def send_sms(self, to: str, body: str) -> dict[str, Any]:
        self.texts.append({"to": to, "body": body, "at": self.now.isoformat()})
        return {"sent": True, "to": to}

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        self.emails.append({"to": to, "subject": subject, "body": body,
                            "at": self.now.isoformat()})
        return {"sent": True, "to": to}

    def notify_technician(self, technician_id: str, subject: str,
                          body: str) -> dict[str, Any]:
        technician = self.technicians.get(technician_id)
        if technician is None:
            raise Refused(
                f"No technician '{technician_id}'. On duty: {sorted(self.technicians)}"
            )
        self.technician_messages.append({
            "technician_id": technician_id, "subject": subject, "body": body,
            "channel": "telegram", "at": self.now.isoformat(),
        })
        return {"sent": True, "to": technician.name, "channel": "telegram"}

    def escalate(self, ticket_id: str, reason: str, details: str) -> dict[str, Any]:
        ticket = self.ticket(ticket_id)
        self.escalations.append({
            "ticket_id": ticket.id, "reason": reason, "details": details,
            "tags": dict(ticket.tags), "at": self.now.isoformat(),
        })
        return {"raised": True, "ticket_id": ticket.id}

    def schedule_followup(self, ticket_id: str, hours: int) -> dict[str, Any]:
        ticket = self.ticket(ticket_id)
        due = self.now + timedelta(hours=int(hours))
        self.followups.append({"ticket_id": ticket.id, "due": due.isoformat(),
                               "answered": False, "asked": 0})
        return {"scheduled": True, "due": due.isoformat()}

    def ticket(self, ticket_id: str) -> Ticket:
        """The ticket, or a refusal naming the ones that exist.

        On the world rather than in the tools because the live world's tickets come out of
        the database, and a tool that indexed `world.tickets` directly would only ever see
        the ones this conversation happened to open.
        """
        found = self.tickets.get(ticket_id)
        if found is None:
            raise Refused(f"No ticket '{ticket_id}'. Open ones: {sorted(self.tickets)}")
        return found

    # ---- what a run is judged on -------------------------------------
    def record(self, kind: str, entry: Any) -> None:
        """A project's own tool, saying something happened that the kit has no word for.

        The alternative is what travel did: return a dict saying `sent: True` and leave no
        trace in the world, so `expect: enquiries: 1` had nothing to count — and, because
        the judge quietly ignored a key it did not know, said nothing about it either. The
        enquiry was never sent in any of fifteen scenarios and the suite scored 13/15.
        """
        self.extras.setdefault(kind, []).append(entry)

    def save(self) -> dict[str, Any]:
        """Everything needed to carry on. Not the same thing as `snapshot()`.

        `snapshot()` is the view a scenario asserts against: what happened, in the words
        the assertions use. It deliberately leaves out the machinery — the id counters, the
        customer book, the times already taken, what has already been done to the outside
        world — because none of that is what a test is asking about.

        Carrying on needs all of it. A conversation resumed with fresh counters mints a
        second TK-0001; one resumed without the idempotency ledger texts the customer
        again, and a crash is precisely when a step retries what it already did.
        """
        return {
            "now": self.now.isoformat(),
            "counters": dict(self._counters),
            "ended": self.ended,
            "end_reason": self.end_reason,
            "customers": {phone: {"phone": c.phone, "name": c.name, "address": c.address,
                                  "email": c.email, "property_type": c.property_type}
                          for phone, c in self.customers.items()},
            "technicians": {i: vars(t) | {"skills": list(t.skills)}
                            for i, t in self.technicians.items()},
            "busy": [[a.isoformat(), b.isoformat()] for a, b in self.busy],
            "tickets": {t.id: {"id": t.id, "status": t.status, "phone": t.phone,
                               "tags": t.tags, "history": t.history}
                        for t in self.tickets.values()},
            "appointments": {a.id: vars(a) | {"starts": a.starts.isoformat()}
                             for a in self.appointments.values()},
            "texts": self.texts, "emails": self.emails,
            "technician_messages": self.technician_messages,
            "escalations": self.escalations, "followups": self.followups,
            "extras": self.extras, "done": self.done, "repeats": self.repeats,
        }

    @classmethod
    def restore(cls, state: dict[str, Any], *, rules: dict[str, Any]) -> "World":
        """The other direction. `save()` had none until conversations needed to survive."""
        world = cls(now=state["now"], rules=rules)
        world._counters = dict(state.get("counters") or {})
        world.ended = bool(state.get("ended"))
        world.end_reason = str(state.get("end_reason") or "")

        world.customers = {phone: Customer(**spec)
                           for phone, spec in (state.get("customers") or {}).items()}
        world.technicians = {
            i: Technician(**(spec | {"skills": tuple(spec.get("skills") or ())}))
            for i, spec in (state.get("technicians") or {}).items()}
        world.busy = [(datetime.fromisoformat(a), datetime.fromisoformat(b))
                      for a, b in (state.get("busy") or [])]
        world.tickets = {i: Ticket(**spec) for i, spec in (state.get("tickets") or {}).items()}
        world.appointments = {
            i: Appointment(**(spec | {"starts": datetime.fromisoformat(spec["starts"])}))
            for i, spec in (state.get("appointments") or {}).items()}

        for key in ("texts", "emails", "technician_messages", "escalations", "followups",
                    "repeats"):
            setattr(world, key, list(state.get(key) or []))
        world.extras = {k: list(v) for k, v in (state.get("extras") or {}).items()}
        world.done = dict(state.get("done") or {})
        return world

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
            **self.extras,
            "repeats": self.repeats,
        }

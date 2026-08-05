"""The simulated world: virtual clock, CRM, calendar, technician state, payments,
outboxes and tickets.

Every tool reads and writes through this object. It is the single assertable source of
truth — once a scenario finishes, the tests inspect the world's final state (ticket
status, calendar entries, messages sent, refunds).

Two design points carry most of the weight:

* **Virtual clock** — the emergency flow needs "one calling round every 10 minutes, six
  rounds, one hour"; waiting an actual hour is not viable in a test suite.
* **Hard gates** — the "must never" rules become executable constraints here. When an
  agent violates one, the tool refuses and a violation is recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from plumbing import config


def _on_duty_roster() -> set[str] | None:
    """Which technicians this machine actually has, or None for all of them.

    The seed carries four, and every scenario and half the tests are written against
    them — so the roster cannot be trimmed in git without rewriting the suite to match a
    deployment. It is trimmed here instead: `PLUMBING_ON_DUTY=t_wang` and the agent can
    only ever pick that one.

    It matters more than it looks. Three of the four have no Telegram, so a booking that
    landed on one of them could not be passed to anybody — the tool refuses and the
    customer is escalated instead of given a time. Narrowing the roster to the people who
    really exist is what stops that being a coin toss.
    """
    import os  # noqa: PLC0415

    listed = os.environ.get("PLUMBING_ON_DUTY", "").strip()
    return {name.strip() for name in listed.split(",") if name.strip()} or None


def _technician_phone(spec: dict[str, Any]) -> str:
    """The seed's number, unless this machine has a real one for that technician.

    The seed is in git and the technicians are real people, so their numbers do not
    belong in it — the ones there are fictional 555 numbers. Production discovered this
    the way these things are always discovered: the dispatch text went out, Twilio
    answered "Landline or unreachable carrier", nothing raised, and the technician simply
    never heard about the job.

    Set `PLUMBING_TECH_PHONE_<ID>` on the machine, upper-cased, e.g.
    `PLUMBING_TECH_PHONE_T_WANG=+16045550101`. Same idea as the credentials in .env: it
    belongs to the deployment, not to the repository.
    """
    import os  # noqa: PLC0415

    override = os.environ.get(f"PLUMBING_TECH_PHONE_{str(spec['id']).upper()}", "").strip()
    return override or str(spec["phone"])


class ToolRejection(Exception):
    """A tool refused to run. Returned to the agent as an error, and recorded as a violation."""

    def __init__(self, message: str, *, violation: str | None = None) -> None:
        super().__init__(message)
        self.violation = violation


# ======================================================================
# Data structures
# ======================================================================


@dataclass
class Technician:
    id: str
    name: str
    phone: str
    skills: list[str]
    areas: list[str]
    max_concurrent_jobs: int
    on_duty: bool
    policy: str = "accept"        # accept | decline | no_answer | accept_after_round:N
    decline_reason: str = ""
    status: str = "idle"          # idle | assigned | en_route | on_site | completed
    active_jobs: int = 0
    status_after_dispatch: str = ""   # scenario hook: departs the moment they are dispatched
    # Learned when they first message the bot. Everything the office sends them goes to
    # Telegram; the phone is only used to make it ring.
    telegram_chat_id: str = ""


@dataclass
class Job:
    job_id: str
    service_type: str
    address: str
    completed_at: str | None
    technician_id: str | None
    status: str
    amount: float | None = None
    warranty_excluded: bool = False
    service_name: str = ""


@dataclass
class Customer:
    phone: str
    name: str = ""
    email: str = ""
    address: str = ""
    area: str = ""
    property_type: str = ""
    jobs: list[Job] = field(default_factory=list)
    is_new: bool = False


@dataclass
class Appointment:
    appointment_id: str
    kind: str                     # standard | emergency | warranty | large_project
    ticket_id: str
    customer_phone: str
    technician_id: str | None
    start: datetime
    duration_minutes: int
    address: str
    description: str
    status: str = "booked"        # booked | rescheduled | cancelled


@dataclass
class Payment:
    payment_id: str
    ticket_id: str
    amount: float
    kind: str                     # deposit
    status: str = "link_sent"     # link_sent | paid | failed | refunded
    link: str = ""
    paid_at: datetime | None = None
    refunded_at: datetime | None = None


@dataclass
class Ticket:
    ticket_id: str
    customer_phone: str = ""
    status: str = "New Inquiry"
    history: list[dict[str, str]] = field(default_factory=list)
    owner_agent: str = ""
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallRecord:
    round: int
    technician_id: str
    called_at: datetime
    connected: bool
    outcome: str                  # accepted | declined | no_answer
    reason: str = ""
    eta_minutes: int | None = None


# ======================================================================
# World
# ======================================================================


class World:
    def __init__(
        self,
        now: str | datetime,
        seed: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
        store: Any = None,
    ) -> None:
        # Optional durable backing. Without one this is the in-memory world every scenario
        # runs against; with one, customers and tickets are read from and written back to
        # the database so they outlive the conversation. Every write-through is guarded by
        # `if self.store`, so the no-store path is byte-for-byte what it always was.
        self.store = store
        self.rules = config.business_rules()
        self.states_cfg = config.ticket_states()
        self.seed = seed or config.world_seed()
        overrides = overrides or {}

        self.tz = ZoneInfo(self.rules["company"]["timezone"])
        self._now = _parse_dt(now, self.tz)

        # --- Technicians ------------------------------------------------
        tech_overrides: dict[str, Any] = overrides.get("technicians", {}) or {}
        self.technicians: dict[str, Technician] = {}
        roster = _on_duty_roster()
        for spec in self.seed.get("technicians", []):
            if roster is not None and spec["id"] not in roster:
                continue
            over = tech_overrides.get(spec["id"], {}) or {}
            self.technicians[spec["id"]] = Technician(
                id=spec["id"],
                name=spec["name"],
                phone=_technician_phone(spec),
                skills=list(spec.get("skills", [])),
                areas=list(spec.get("areas", [])),
                max_concurrent_jobs=spec.get("max_concurrent_jobs", 3),
                on_duty=over.get("on_duty", spec.get("on_duty", True)),
                policy=over.get("policy", spec.get("default_policy", "accept")),
                decline_reason=over.get("decline_reason", ""),
                status=over.get("status", "idle"),
                telegram_chat_id=spec.get("telegram_chat_id", ""),
            )
            # A scenario can have a technician set off the moment they are dispatched,
            # which is what puts the automatic refund out of reach.
            self.technicians[spec["id"]].status_after_dispatch = over.get(
                "status_after_dispatch", ""
            )

        # --- CRM --------------------------------------------------------
        self.customers: dict[str, Customer] = {}
        for spec in self.seed.get("customers", []):
            phone = normalize_phone(spec["phone"])
            self.customers[phone] = Customer(
                phone=phone,
                name=spec.get("name", ""),
                email=spec.get("email", ""),
                address=spec.get("address", ""),
                area=spec.get("area", ""),
                property_type=spec.get("property_type", ""),
                jobs=[
                    Job(
                        job_id=j["job_id"],
                        service_type=j["service_type"],
                        address=j.get("address", ""),
                        completed_at=j.get("completed_at"),
                        technician_id=j.get("technician_id"),
                        status=j.get("status", "completed"),
                        amount=j.get("amount"),
                        warranty_excluded=j.get("warranty_excluded", False),
                        service_name=j.get("service_name", ""),
                    )
                    for j in spec.get("jobs", [])
                ],
            )

        # --- Prior chat transcripts -------------------------------------
        self.chat_history: list[dict[str, Any]] = [
            {**entry, "phone": normalize_phone(entry.get("phone", ""))}
            for entry in self.seed.get("chat_history", [])
        ]
        self.chat_history.extend(overrides.get("chat_history", []) or [])

        # --- Calendar ---------------------------------------------------
        self.appointments: list[Appointment] = []
        for index, spec in enumerate(self.seed.get("existing_appointments", [])):
            self.appointments.append(
                Appointment(
                    appointment_id=f"PRE-{index + 1}",
                    kind="standard",
                    ticket_id="",
                    customer_phone="",
                    technician_id=spec["technician_id"],
                    start=_parse_dt(spec["start"], self.tz),
                    duration_minutes=spec.get("duration_minutes", 120),
                    address="",
                    description="Pre-existing appointment",
                )
            )

        # --- Payments ---------------------------------------------------
        payment_cfg = dict(self.seed.get("payment", {}))
        payment_cfg.update(overrides.get("payment", {}) or {})
        self.payment_behavior = payment_cfg
        self.payments: dict[str, Payment] = {}

        # --- Everything else --------------------------------------------
        self.tickets: dict[str, Ticket] = {}
        self.sms_outbox: list[dict[str, Any]] = []
        self.email_outbox: list[dict[str, Any]] = []
        # Photos, video and drawings arrive as replies to an email we send, so the
        # customer's own mailbox becomes part of their record rather than a link that
        # expires and a file store nobody can trace back to a person.
        self.material_requests: list[dict[str, Any]] = []
        self.received_materials: list[dict[str, Any]] = []
        self.call_records: list[CallRecord] = []
        self.warranty_reviews: dict[str, dict[str, Any]] = {}
        self.job_outcomes: dict[str, dict[str, Any]] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.followups: list[dict[str, Any]] = []
        self.escalations: list[dict[str, Any]] = []
        self.tool_log: list[dict[str, Any]] = []
        self.violations: list[dict[str, Any]] = []
        self.handoffs: list[dict[str, Any]] = []
        # What the customer will send back, and how long they take.
        self.material_behavior: dict[str, Any] = overrides.get("materials", {}) or {}
        # What the technician reports back after attending, and how long they take.
        self.job_outcome_behavior: dict[str, Any] = overrides.get("job_outcome", {}) or {}
        # How the original technician will rule on a warranty claim, and how long
        # they take. Scenario-controlled so the verdict is deterministic.
        self.warranty_review_behavior: dict[str, Any] = overrides.get("warranty_review", {}) or {}

        self._counters: dict[str, int] = {}
        self.active_ticket_id: str = ""

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------
    def now(self) -> datetime:
        return self._now

    def advance(self, minutes: int) -> datetime:
        self._now += timedelta(minutes=minutes)
        return self._now

    def is_holiday(self, day: date) -> dict[str, Any] | None:
        for entry in self.rules["schedule"].get("public_holidays", []):
            if str(entry["date"]) == day.isoformat():
                return entry
        return None

    def is_working_day(self, day: date) -> bool:
        if day.weekday() not in self.rules["schedule"]["working_days"]:
            return False
        return self.is_holiday(day) is None

    def working_window(self, day: date) -> tuple[datetime, datetime]:
        hours = self.rules["schedule"]["working_hours"]
        return (
            datetime.combine(day, _parse_time(hours["start"]), tzinfo=self.tz),
            datetime.combine(day, _parse_time(hours["end"]), tzinfo=self.tz),
        )

    def day_context(self, when: datetime | None = None) -> dict[str, Any]:
        """Calendar context for a moment in time — drives rate bands and booking rules."""
        moment = when or self._now
        day = moment.date()
        holiday = self.is_holiday(day)
        start, end = self.working_window(day)
        night_at = datetime.combine(
            day,
            _parse_time(self.rules["schedule"]["night_starts_at"]),
            tzinfo=self.tz,
        )
        weekday_names = [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ]
        return {
            "datetime": moment.isoformat(),
            "date": day.isoformat(),
            "weekday": weekday_names[moment.weekday()],
            "is_sunday": moment.weekday() == 6,
            "is_public_holiday": holiday is not None,
            "holiday_name": holiday["name"] if holiday else None,
            "is_working_day": self.is_working_day(day),
            "within_business_hours": self.is_working_day(day) and start <= moment < end,
            "is_night": moment >= night_at,
            "business_hours": f"{self.rules['schedule']['working_hours']['start']}-"
            f"{self.rules['schedule']['working_hours']['end']}",
            "standard_booking_available": self.is_working_day(day),
        }

    def emergency_fee_tier(self, when: datetime | None = None) -> dict[str, Any]:
        """Select the applicable emergency rate band. Every figure comes from the rules file."""
        ctx = self.day_context(when)
        tiers = {
            t["id"]: t
            for t in self.rules["pricing"]["emergency_inspection_fee"]["tiers"]
        }
        if ctx["is_sunday"] or ctx["is_public_holiday"]:
            tier = tiers["sunday_or_holiday"]
        elif ctx["is_night"]:
            tier = tiers["night_after_18"]
        elif ctx["within_business_hours"]:
            tier = tiers["workday_business_hours"]
        else:
            tier = tiers["workday_offhours_before_18"]
        fee_cfg = self.rules["pricing"]["emergency_inspection_fee"]
        return {
            "tier_id": tier["id"],
            "amount": tier["amount"],
            "currency": fee_cfg["currency"],
            "qualifier": fee_cfg["qualifier"],
            "condition": tier["condition"],
            "context": ctx,
        }

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        # The in-memory counter restarts at 1 every process. In production that hands two
        # different customers the same ticket number, so the database decides instead.
        if self.store is not None:
            return self.store.next_id(prefix)
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    def create_ticket(self, phone: str = "") -> Ticket:
        ticket = Ticket(
            ticket_id=self.next_id("TK"),
            customer_phone=normalize_phone(phone) if phone else "",
            status=self.states_cfg["initial"],
        )
        ticket.history.append(
            {"at": self._now.isoformat(), "status": ticket.status, "note": "Ticket created"}
        )
        self.tickets[ticket.ticket_id] = ticket
        if not self.active_ticket_id:
            self.active_ticket_id = ticket.ticket_id
        self._persist_ticket(ticket, "ticket_created")
        return ticket

    def seed_ticket(
        self, status: str, phone: str = "", fields: dict[str, Any] | None = None
    ) -> Ticket:
        """Create a ticket already partway through its life, for scenario setup.

        A scenario that enters at a non-entry agent is modelling a handoff that already
        happened. Without this the agent starts at New Inquiry and spends its turns
        climbing the state machine instead of doing the job under test.

        Deliberately bypasses transition validation: this is fixture setup, not an agent
        action. Agents still go through transition_ticket and are still checked.
        """
        if status not in set(self.states_cfg["states"]):
            raise ValueError(f"'{status}' is not a valid ticket status")
        ticket = self.create_ticket(phone)
        ticket.status = status
        ticket.tags.update(fields or {})
        ticket.history.append(
            {"at": self._now.isoformat(), "status": status, "note": "Seeded by scenario setup"}
        )
        return ticket

    def get_ticket(self, ticket_id: str) -> Ticket:
        ticket = self.tickets.get(ticket_id)
        if not ticket:
            raise ToolRejection(f"Ticket {ticket_id} does not exist. Call ticket.create first.")
        return ticket

    def active_ticket(self) -> Ticket | None:
        return self.tickets.get(self.active_ticket_id) if self.active_ticket_id else None

    def transition_ticket(self, ticket_id: str, target: str, note: str = "") -> Ticket:
        """Validate the transition against the state machine. Illegal jumps are rejected."""
        ticket = self.get_ticket(ticket_id)
        valid_states = set(self.states_cfg["states"])
        if target not in valid_states:
            raise ToolRejection(
                f"'{target}' is not a valid status.",
                violation="invalid_ticket_state",
            )
        if ticket.status == target:
            return ticket

        allowed = set(self.states_cfg["transitions"].get(ticket.status, []))
        allowed |= set(self.states_cfg.get("universal_targets", []))
        if target not in allowed:
            raise ToolRejection(
                f"Cannot go straight from '{ticket.status}' to '{target}'. "
                f"Allowed next states: {sorted(allowed)}. Do not skip key states.",
                violation="illegal_ticket_transition",
            )

        ticket.status = target
        ticket.history.append(
            {"at": self._now.isoformat(), "status": target, "note": note}
        )
        self._persist_ticket(ticket, "status_changed", detail=target)
        return ticket

    def find_customer(self, phone: str) -> "Customer | None":
        """Memory first, then the database — and hydrate, so later reads are local.

        Without a store this is exactly the old dictionary lookup against the seed. With
        one, a customer who called last month is found, which is the single thing the
        whole persistence layer exists for.
        """
        key = normalize_phone(phone)
        found = self.customers.get(key)
        if found is not None or self.store is None:
            return found
        row = self.store.find_customer(key)
        if row is None:
            return None
        customer = Customer(
            phone=row["phone"], name=row.get("name", ""), email=row.get("email", ""),
            address=row.get("address", ""), area=row.get("area", ""),
            property_type=row.get("property_type", ""),
            jobs=[Job(
                job_id=j["job_id"], service_type=j.get("service_type", ""),
                service_name=j.get("service_name", ""), address=j.get("address", ""),
                completed_at=j.get("completed_at"), technician_id=j.get("technician_id"),
                status=j.get("status", "completed"), amount=j.get("amount"),
                warranty_excluded=bool(j.get("warranty_excluded")),
            ) for j in row.get("jobs", [])],
        )
        self.customers[normalize_phone(row["phone"])] = customer
        return customer

    def save_customer(self, customer: "Customer") -> None:
        self.customers[normalize_phone(customer.phone)] = customer
        if self.store is None:
            return
        self.store.upsert_customer(
            customer.phone, name=customer.name, email=customer.email,
            address=customer.address, area=customer.area,
            property_type=customer.property_type,
        )

    def _persist_ticket(self, ticket: Ticket, event: str, detail: str = "") -> None:
        if self.store is None:
            return
        self.store.save_ticket(ticket)
        self.store.add_event(event, ticket_id=ticket.ticket_id, detail=detail)

    def _persist_appointment(self, appointment: Appointment, event: str) -> None:
        if self.store is None:
            return
        self.store.save_appointment(appointment)
        # `kind` is add_event's own first parameter, so the appointment's kind travels
        # under a different name. Passing both collided and only ever fired when a store
        # was attached, which is why the whole suite stayed green over it.
        self.store.add_event(
            event, ticket_id=appointment.ticket_id, detail=appointment.appointment_id,
            appointment_kind=appointment.kind, start=appointment.start.isoformat(),
            technician_id=appointment.technician_id,
        )

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------
    def technician_busy(self, tech_id: str, start: datetime, minutes: int) -> bool:
        end = start + timedelta(minutes=minutes)
        for appt in self.appointments:
            if appt.technician_id != tech_id or appt.status == "cancelled":
                continue
            appt_end = appt.start + timedelta(minutes=appt.duration_minutes)
            if start < appt_end and appt.start < end:
                return True
        return False

    def external_busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Everything booked in the window that this process does not hold in memory.

        Two sources, and both matter for different reasons. The **database** holds what was
        booked in earlier conversations — without it every new conversation believes the
        diary is empty and sells the same slot again. The **real calendar** holds whatever
        a person put there by hand, and a technician who blocked out Thursday afternoon in
        their own phone is exactly the appointment nobody here knows about.

        Fetched once per search, for the whole window. Asking per candidate slot would be
        hundreds of calls to answer one question.
        """
        busy: list[tuple[datetime, datetime]] = []

        if self.store is not None:
            for row in self.store.appointments_between(start, end):
                began = _parse_dt(row["start_at"], self.tz)
                busy.append((began, began + timedelta(minutes=int(row["duration_minutes"] or 0))))

        from plumbing.integrations import is_live  # noqa: PLC0415

        if is_live("calendar.find_slots"):
            from plumbing.integrations import google_calendar  # noqa: PLC0415
            from plumbing.integrations.gate import LiveToolUnavailable  # noqa: PLC0415

            try:
                busy.extend(google_calendar.busy_periods(start, end))
            except LiveToolUnavailable as exc:
                # Offering a slot without knowing what is really in the diary is how two
                # jobs end up at the same hour. Better to say we cannot check.
                raise ToolRejection(
                    f"The calendar could not be read, so I cannot tell you what is free: "
                    f"{exc}. Do not offer a time. Escalate instead."
                ) from exc

        return busy

    def find_slots(
        self,
        *,
        area: str = "",
        skill: str = "",
        duration_minutes: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Find the earliest standard appointment slots, skipping Sundays and holidays."""
        sched = self.seed.get("scheduling", {})
        duration = duration_minutes or sched.get("default_job_duration_minutes", 120)
        limit = limit or sched.get("max_slots_to_offer", 3)
        granularity = sched.get("slot_granularity_minutes", 60)
        horizon = sched.get("search_horizon_days", 14)

        candidates = [
            t
            for t in self.technicians.values()
            if t.on_duty
            and (not area or area in t.areas)
            and (not skill or skill in t.skills)
            and t.active_jobs < t.max_concurrent_jobs
        ]
        if not candidates:
            candidates = [t for t in self.technicians.values() if t.on_duty]

        slots: list[dict[str, Any]] = []
        cursor_day = self._now.date()

        # One lookup for the whole search window, then a plain overlap test per slot.
        window_end = self._now + timedelta(days=horizon + 1)
        booked_elsewhere = self.external_busy(self._now, window_end)

        def taken(at: datetime, minutes: int) -> bool:
            finish = at + timedelta(minutes=minutes)
            return any(at < b_end and finish > b_start for b_start, b_end in booked_elsewhere)

        for offset in range(horizon):
            day = cursor_day + timedelta(days=offset)
            if not self.is_working_day(day):
                continue
            start_of_day, end_of_day = self.working_window(day)
            slot = start_of_day
            # Today's search starts after "now", rounded up to the next granularity step
            if offset == 0 and self._now > slot:
                minutes_past = (self._now - slot).total_seconds() / 60
                steps = int(minutes_past // granularity) + 1
                slot = start_of_day + timedelta(minutes=steps * granularity)
            while slot + timedelta(minutes=duration) <= end_of_day:
                if taken(slot, duration):
                    slot += timedelta(minutes=granularity)
                    continue
                for tech in candidates:
                    if not self.technician_busy(tech.id, slot, duration):
                        slots.append(
                            {
                                "start": slot.isoformat(),
                                "end": (slot + timedelta(minutes=duration)).isoformat(),
                                "technician_id": tech.id,
                                "technician_name": tech.name,
                            }
                        )
                        break
                if len(slots) >= limit:
                    return slots
                slot += timedelta(minutes=granularity)
        return slots

    def create_appointment(
        self,
        *,
        kind: str,
        ticket_id: str,
        phone: str,
        start: datetime,
        technician_id: str | None,
        address: str,
        description: str,
        duration_minutes: int | None = None,
    ) -> Appointment:
        sched = self.seed.get("scheduling", {})
        duration = duration_minutes or sched.get("default_job_duration_minutes", 120)

        # Hard gate: no emergency dispatch before the deposit has been paid
        # Hard gate: nothing gets booked into a property our insurance does not cover.
        # This is the only failure left in the intake+small_job pair that carries real
        # liability rather than lost business, so it does not live in a prompt. Three
        # prompts carry the rule as guidance and the tool refuses regardless.
        #
        # **Before the deposit check, deliberately.** The other order told the agent to
        # go and collect a deposit for a job that would then be refused on the next
        # call — the customer pays for something we can never do and then has to be
        # refunded. "We cannot take this at all" is the more useful thing to hear
        # first, and it is true whether or not any money has moved.
        excluded = self._excluded_property(ticket_id, kind)
        if excluded:
            raise ToolRejection(
                f"{excluded['reason'].strip()} Do not book this. {excluded['exception'].strip()}",
                violation="excluded_property_type",
            )

        if kind == "emergency" and self.rules["emergency_dispatch"].get(
            "deposit_required_before_dispatch", True
        ):
            if not self.deposit_paid(ticket_id):
                raise ToolRejection(
                    "The deposit has not been paid, so no emergency dispatch can be created. "
                    "Send the CAD 100 deposit link with payment.send_deposit_link and confirm "
                    "payment first.",
                    violation="dispatch_before_deposit",
                )

        # Hard gate: no standard bookings on Sundays or public holidays
        if kind in ("standard", "warranty") and not self.is_working_day(start.date()):
            ctx = self.day_context(start)
            label = ctx["holiday_name"] or ("Sunday" if ctx["is_sunday"] else "a closed day")
            raise ToolRejection(
                f"{start.date().isoformat()} is {label}; standard appointments are not "
                f"available, only emergency service. Offer the next normal working day.",
                violation="booking_on_closed_day",
            )

        appointment = Appointment(
            appointment_id=self.next_id("AP"),
            kind=kind,
            ticket_id=ticket_id,
            customer_phone=normalize_phone(phone),
            technician_id=technician_id,
            start=start,
            duration_minutes=duration,
            address=address,
            description=description,
        )
        self.appointments.append(appointment)
        self._persist_appointment(appointment, "appointment_booked")
        if technician_id and technician_id in self.technicians:
            tech = self.technicians[technician_id]
            tech.active_jobs += 1
            if kind == "emergency":
                tech.status = tech.status_after_dispatch or "assigned"
        return appointment

    def get_appointment(self, appointment_id: str) -> Appointment:
        for appt in self.appointments:
            if appt.appointment_id == appointment_id:
                return appt
        raise ToolRejection(f"Appointment {appointment_id} does not exist.")

    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------
    def deposit_paid(self, ticket_id: str) -> bool:
        return any(
            p.ticket_id == ticket_id and p.kind == "deposit" and p.status == "paid"
            for p in self.payments.values()
        )

    def find_deposit(self, ticket_id: str) -> Payment | None:
        for payment in self.payments.values():
            if payment.ticket_id == ticket_id and payment.kind == "deposit":
                return payment
        return None

    def _excluded_property(self, ticket_id: str, kind: str) -> dict[str, Any] | None:
        """The excluded-property rule that blocks this booking, or None.

        Reads what the agent recorded on the ticket rather than taking an argument, so a
        booking cannot slip past by simply not mentioning the property type.

        Which flows are excepted comes from each rule's `except_for` list rather than from
        this function. It used to be two hardcoded conditions here and a prose note in the
        rules file, and the note drifted: it said the exclusion applied to small jobs,
        which read as covering emergencies too and disagreed with what this actually did.
        A rule the gate enforces and a rule the file states have to be one rule.

        Today that list is a large project — reviewed by a person before we commit — and a
        warranty visit, which goes wherever the original job was, because we already worked
        there and the insurance question was settled then.

        A property type nobody recorded is **not** blocked here. Booking without one is a
        different fault, and turning this gate into "no property type, no appointment"
        would make it fire on flows that never had a property to record. That check
        belongs with whoever decides bookings must carry one.
        """
        ticket = self.tickets.get(ticket_id)
        if ticket is None:
            return None
        tags = ticket.tags or {}
        property_type = str(tags.get("property_type", "")).strip().lower()
        if not property_type:
            return None

        # What this booking is, in the vocabulary `except_for` uses. `kind` is the
        # appointment kind; the size of the job lives on the ticket instead.
        category = str(tags.get("category", "")).strip().lower()
        flows = {kind, "large_job"} if "large" in category else {kind}

        for rule in self.rules.get("service_policy", {}).get("excluded_property_types", []):
            if rule["id"] == property_type and not (set(rule.get("except_for") or []) & flows):
                return rule
        return None

    def dispatch_confirmed(self, ticket_id: str) -> bool:
        """Has the customer been told a technician is coming? Decides refund eligibility.

        A technician who accepts an emergency call sets off immediately, so the moment that
        confirmation lands they are already on the road. Waiting for an "en route" status
        would leave a window where the van is moving but the system still thinks a refund is
        free — which is exactly the window a customer cancels in.
        """
        ticket = self.tickets.get(ticket_id)
        phone = ticket.customer_phone if ticket else ""
        for message in self.sms_outbox:
            if (
                message.get("purpose") == "emergency_confirmation"
                and message.get("recipient_type") == "customer"
                and (not phone or message.get("to") == phone)
            ):
                return True
        # A technician already moving counts too, however it came about.
        for appt in self.appointments:
            if appt.ticket_id != ticket_id or not appt.technician_id:
                continue
            tech = self.technicians.get(appt.technician_id)
            if tech and tech.status in ("en_route", "on_site"):
                return True
        return False

    def refund_deposit(self, ticket_id: str) -> Payment:
        payment = self.find_deposit(ticket_id)
        if not payment:
            raise ToolRejection(
                f"Ticket {ticket_id} has no deposit on record, so there is nothing to refund."
            )
        if payment.status == "refunded":
            raise ToolRejection("This deposit has already been refunded. Do not refund twice.")
        if payment.status != "paid":
            raise ToolRejection(
                f"The deposit is currently '{payment.status}', not paid, so no refund is due."
            )
        # Hard gate: no automatic refund once the customer has been told someone is coming
        if self.dispatch_confirmed(ticket_id):
            raise ToolRejection(
                "The customer has already been sent the emergency confirmation, which means "
                "the technician is on the road. You must not refund automatically — use "
                "escalate.raise for supervisor review instead.",
                violation="auto_refund_after_dispatch_confirmed",
            )
        payment.status = "refunded"
        payment.refunded_at = self._now
        return payment

    # ------------------------------------------------------------------
    def record_violation(self, kind: str, detail: str, tool: str = "") -> None:
        self.violations.append(
            {"kind": kind, "detail": detail, "tool": tool, "at": self._now.isoformat()}
        )

    def snapshot(self) -> dict[str, Any]:
        """Final world state, consumed by the assertions."""
        return {
            "now": self._now.isoformat(),
            "tickets": {
                t.ticket_id: {
                    "status": t.status,
                    "customer_phone": t.customer_phone,
                    "owner_agent": t.owner_agent,
                    "history": t.history,
                    "tags": t.tags,
                }
                for t in self.tickets.values()
            },
            "active_ticket_id": self.active_ticket_id,
            "appointments": [
                {
                    "id": a.appointment_id,
                    "kind": a.kind,
                    "ticket_id": a.ticket_id,
                    "technician_id": a.technician_id,
                    "start": a.start.isoformat(),
                    "status": a.status,
                    "address": a.address,
                }
                for a in self.appointments
                if a.ticket_id  # exclude seeded pre-existing appointments
            ],
            "payments": [
                {
                    "id": p.payment_id,
                    "ticket_id": p.ticket_id,
                    "kind": p.kind,
                    "amount": p.amount,
                    "status": p.status,
                }
                for p in self.payments.values()
            ],
            "sms_outbox": self.sms_outbox,
            "email_outbox": self.email_outbox,
            "call_records": [
                {
                    "round": c.round,
                    "technician_id": c.technician_id,
                    "outcome": c.outcome,
                    "connected": c.connected,
                    "reason": c.reason,
                    "at": c.called_at.isoformat(),
                }
                for c in self.call_records
            ],
            "warranty_reviews": list(self.warranty_reviews.values()),
            "job_outcomes": list(self.job_outcomes.values()),
            "quotes": list(self.quotes.values()),
            "followups": self.followups,
            "material_requests": self.material_requests,
            "received_materials": self.received_materials,
            "escalations": self.escalations,
            "handoffs": self.handoffs,
            "violations": self.violations,
            "customers_created": [
                c.phone for c in self.customers.values() if c.is_new
            ],
        }


# ======================================================================
# Helpers
# ======================================================================


def normalize_phone(raw: str) -> str:
    """Normalise to +1XXXXXXXXXX so CRM lookups are not defeated by formatting."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits if digits else ""


def is_valid_phone(raw: str) -> bool:
    """North American numbers: 10 digits, or 11 starting with 1."""
    digits = re.sub(r"\D", "", str(raw or ""))
    return len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))


def _parse_dt(value: str | datetime, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _parse_time(value: str) -> time:
    hour, _, minute = str(value).partition(":")
    return time(int(hour), int(minute or 0))

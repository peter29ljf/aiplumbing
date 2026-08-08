"""The vocabulary both worlds speak, and the one exception either of them raises.

The sixteen tools in `flow/sim/tools.py` are written once and run against two backends: a
simulator that keeps everything in memory (`flow/sim/world.py`) and a live world that
writes to the database and calls real services (`flow/live/world.py`). That only works if
there is one set of words for a customer, a ticket, an appointment — otherwise the tools
would have to know which world they were in, and the whole point of the split is that they
do not.

**A backend has to provide all of this.** The tools reach for nothing else:

    rules                                what things cost, read from business_rules.yaml
    now                                  the moment this conversation is happening in
    technicians          dict[str, Technician]
    tickets              dict[str, Ticket]           mutable; `tags` is the memory
    open_ticket(phone) -> Ticket
    set_status(ticket_id, status)
    find_customer(phone) -> Customer | None
    add_customer(phone, name, address, email) -> Customer
    free_slots() -> list[datetime]
    find_appointments(phone) -> list[Appointment]
    book(ticket_id, starts, minutes, technician, address, what, phone) -> Appointment
    send_sms(to, body) -> dict
    send_email(to, subject, body) -> dict
    notify_technician(technician_id, subject, body) -> dict
    escalate(ticket_id, reason, details) -> dict
    schedule_followup(ticket_id, hours) -> dict

The last five are the ones that reach outside the process in the live world, and they are
methods rather than list appends for exactly that reason: appending to `world.texts` is
something a tool can do, sending a text is something only the world knows how to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "AnyWorld", "Appointment", "Customer", "Job", "Refused", "Technician", "Ticket",
    "emergency_tier", "phone_key",
]


# What the tools annotate their first argument with. Deliberately not a base class and
# not a Protocol: the contract is the list in the docstring above plus the two
# implementations, and a Protocol here would be a third copy of that list to keep in step
# with the other two. Neither world inherits from anything; they simply both answer to it.
AnyWorld = Any


class Refused(Exception):
    """This cannot be done, and the model is told why so it can try something else.

    Two kinds of thing end up here and they read the same to the model, which is right:

    - It was called with something nobody can act on — a ticket id that does not exist,
      a technician who is not on the roster, an empty message.
    - The real service would not do it. Telegram is unreachable, the calendar rejected
      the booking, the credentials for a tool that is switched on are missing.

    Both mean *it did not happen*, and that is the only fact the conversation needs. A
    failure swallowed here becomes a customer told a technician is coming.
    """


def phone_key(number: str) -> str:
    """Last ten digits. `+1 (604) 721-8629` and `6047218629` are one customer."""
    return re.sub(r"\D", "", number or "")[-10:]


def emergency_tier(rules: dict[str, Any], now: datetime) -> dict[str, Any]:
    """What an emergency call-out costs at this moment, and which band that is.

    A pure function of the rules file and the clock, so both worlds get the same answer
    and neither has to hold the logic.

    **Order matters and is in the rules file, not here.** A statutory holiday falling on a
    Tuesday morning matches both `sunday_or_holiday` and `workday_business_hours`, and the
    difference is two hundred dollars; `tier_precedence` settles it.

    The `condition` comes back with the amount because a customer told "CAD 400" and not
    told why hears a number somebody made up. Told "it is after 6pm, so it is CAD 400",
    they can check it against their own watch.
    """
    fee = rules["pricing"]["emergency_inspection_fee"]
    tiers = {t["id"]: t for t in fee["tiers"]}
    schedule = rules["schedule"]

    opens = _hour(schedule["working_hours"]["start"])
    closes = _hour(schedule["working_hours"]["end"])
    night_from = _hour(schedule.get("night_starts_at") or schedule["working_hours"]["end"])
    holidays = {str(h.get("date") if isinstance(h, dict) else h)
                for h in schedule.get("public_holidays", [])}

    is_holiday = now.date().isoformat() in holidays
    is_sunday = now.weekday() == 6

    matched = {
        "sunday_or_holiday": is_sunday or is_holiday,
        "night_after_18": now.hour >= night_from,
        "workday_business_hours": opens <= now.hour < closes,
        # Everything left over on a working day: before opening, and the gap between
        # closing and the night rate if the two are ever set apart.
        "workday_offhours_before_18": True,
    }
    chosen = next(tier_id for tier_id in fee["tier_precedence"]
                  if matched.get(tier_id) and tier_id in tiers)
    tier = tiers[chosen]

    return {
        "tier": chosen,
        "amount": tier["amount"],
        "currency": fee["currency"],
        "qualifier": fee.get("qualifier", ""),
        "condition": tier["condition"],
        "right_now": f"{now.strftime('%A')} {now.strftime('%-I:%M %p')}",
    }


def _hour(value: Any) -> int:
    return int(str(value).split(":")[0])


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
    # Work of theirs that is still open — a ticket from a conversation last week, and what
    # it was about. Somebody we already have half a job for should not be started from
    # nothing, and the lookup is the only place a step ever sees their past.
    #
    # Deliberately *not* their past messages. A transcript would be the whole conversation
    # back in every prompt, which is the thing this design exists to avoid; what a later
    # step can act on is the conclusion, not the words that reached it.
    open_work: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Technician:
    id: str
    name: str
    telegram: str = ""
    phone: str = ""
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

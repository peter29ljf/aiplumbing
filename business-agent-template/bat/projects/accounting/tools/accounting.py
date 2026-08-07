"""Nine tools written for Chen & Associates CPA.

The kit's service-dispatch tools assume somebody goes out to a street address and a
technician is dispatched. This business is the opposite: the client comes to us or meets
on video, nobody goes out, and the person the work goes to is one manager, Michelle. So
the calendar, the client record and the notify tool look different here.

Rules that are enforced in code rather than prose, because prose is followed most of the
time and code is followed every time:

- the bookkeeper is never assignable — the diary searches CPA diaries only;
- Saturdays are open only in tax season (1 Feb - 30 Apr);
- Sundays, evenings and BC statutory holidays are closed;
- a corporate year-end is never quoted — only the "starting at" floor and the sentence
  that a CPA quotes it after seeing the books;
- which personal tier applies is decided here, not in prose;
- deadline urgency is computed, not felt;
- handovers have exactly one recipient, Michelle — no id argument to invent.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bat.runtime.registry import Refused, _ticket, tool
from bat.runtime.world import AnyWorld

# BC statutory holidays (fixed dates + the movable ones surfaced by the simulator).
# The world's own calendar decides which are closed; this list is the fallback set the
# diary uses to refuse a closed slot when the simulator does not already.
_BC_HOLIDAYS_MONTH_DAY = {
    "01-01",  # New Year's Day
    "02-17",  # Family Day (third Monday of February, 2025)
    "04-18",  # Good Friday (2025)
    "05-19",  # Victoria Day (third Monday of May, 2025)
    "07-01",  # Canada Day
    "08-04",  # BC Day (first Monday of August, 2025)
    "09-01",  # Labour Day (first Monday of September, 2025)
    "10-13",  # Thanksgiving (second Monday of October, 2025)
    "11-11",  # Remembrance Day
    "12-25",  # Christmas Day
    "12-26",  # Boxing Day
}


def _is_open(world: AnyWorld, when: datetime) -> bool:
    """Is this moment inside the firm's opening hours?"""
    rules = world.rules
    hours = rules["hours"]
    if when.weekday() < 5:
        start = hours["open_weekday"]["start"]
        end = hours["open_weekday"]["end"]
    else:
        # Saturday only, and only in tax season.
        season = hours["saturday_season"]
        md = when.strftime("%m-%d")
        if md < season["from"] or md > season["to"]:
            return False
        if when.weekday() != 5:
            return False
        start = hours["saturday"]["start"]
        end = hours["saturday"]["end"]
    t = when.strftime("%H:%M")
    return start <= t < end


@tool(
    "client.create",
    "Open a record for a new client. Unlike the general create, there is no street address "
    "to ask for — this firm's clients come to us or meet on video — and it records the "
    "language they prefer and whether it is a personal or corporate matter. Returns the "
    "record so the reply can use the name.",
    {
        "phone": {"type": "string"},
        "name": {"type": "string"},
        "email": {"type": "string"},
        "language": {"type": "string", "description": "Their preferred language, e.g. English or Mandarin"},
        "matter": {"type": "string", "description": "personal or corporate"},
    },
    remembers=("phone", "name", "email", "language", "matter"),
)
def client_create(world: AnyWorld, phone: str, name: str, email: str,
                  language: str, matter: str) -> dict[str, Any]:
    customer = world.add_customer(phone=phone, name=name, email=email)
    return {"created": True, "phone": customer.phone, "name": customer.name,
            "language": language, "matter": matter}


@tool(
    "client.work_in_progress",
    "What we hold in hand for an existing client and what state it is in. The "
    "\"where is my return\" check — whether we have a file in progress for them and what "
    "it needs.",
    {"phone": {"type": "string"}},
    remembers=("has_in_progress", "in_progress_detail"),
)
def client_work_in_progress(world: AnyWorld, phone: str) -> dict[str, Any]:
    customer = world.find_customer(phone)
    if customer is None:
        return {"has_in_progress": False, "in_progress_detail": ""}
    jobs = getattr(customer, "jobs", []) or []
    open_jobs = [j for j in jobs if getattr(j, "what", "") and not getattr(j, "finished_on", None)]
    if open_jobs:
        return {"has_in_progress": True,
                "in_progress_detail": open_jobs[0].what}
    return {"has_in_progress": False, "in_progress_detail": ""}


@tool(
    "rules.get_fees",
    "Everything about money this enquiry may need, in one call: the simple and the "
    "rental-or-self-employment personal-return figures, the corporate year-end starting-at "
    "figure with the sentence that a CPA quotes the real total after seeing the books, the "
    "bookkeeping hourly rate, the free half hour, and the no-late-surcharge line. One call, "
    "so a step told to talk about money is never left with nothing to look up.",
    {},
    remembers=("free_consultation",),
)
def rules_get_fees(world: AnyWorld) -> dict[str, Any]:
    rules = world.rules
    p = rules["pricing"]
    return {
        "currency": rules["firm"]["currency"],
        "personal": {
            "simple": p["personal_return"]["simple"],
            "rental_or_selfemployment": p["personal_return"]["rental_or_selfemployment"],
            "qualifier": p["personal_return"]["qualifier"],
        },
        "corporate": {
            "starting_at": p["corporate_year_end"]["starting_at"],
            "qualifier": p["corporate_year_end"]["qualifier"],
            "quote_sentence": "A CPA quotes the actual figure after seeing the books — never over chat.",
        },
        "bookkeeping": {"hourly": p["bookkeeping"]["hourly"]},
        "consultation": {
            "minutes": p["consultation"]["minutes"],
            "free": p["consultation"]["free"],
            "wording": p["consultation"]["free_wording"],
        },
        "late_filing": {"wording": p["late_filing"]["no_surcharge_wording"]},
    }


@tool(
    "rules.price_personal_return",
    "Which personal-return tier applies and the figure to quote. Decide this in code, not "
    "prose: give it whether there is rental or self-employment income and it returns the "
    "flat fee.",
    {"rental_or_selfemployment": {"type": "boolean"}},
    remembers=("tier", "quoted_fee"),
)
def rules_price_personal(world: AnyWorld, rental_or_selfemployment: bool) -> dict[str, Any]:
    p = world.rules["pricing"]["personal_return"]
    if rental_or_selfemployment:
        return {"tier": "rental_or_selfemployment", "fee": p["rental_or_selfemployment"],
                "qualifier": p["qualifier"]}
    return {"tier": "simple", "fee": p["simple"], "qualifier": p["qualifier"]}


@tool(
    "rules.get_decline",
    "The firm's exact wording for each of the three things we turn away — US tax filings "
    "(with the cross-border firm's name and number), a CRA audit already in progress, and "
    "crypto trading gains. Returned verbatim, because the agent repeats these nearly word "
    "for word.",
    {"kind": {"type": "string", "description": "us_tax, cra_audit, or crypto"}},
)
def rules_get_decline(world: AnyWorld, kind: str) -> dict[str, Any]:
    refusals = world.rules["refusals"]
    if kind == "us_tax":
        r = refusals["us_tax"]
        return {
            "kind": kind,
            "wording": r["wording"].format(name=r["referral_name"], phone=r["referral_phone"]),
            "referral_name": r["referral_name"],
            "referral_phone": r["referral_phone"],
        }
    if kind == "cra_audit":
        return {"kind": kind, "wording": refusals["cra_audit"]["wording"]}
    if kind == "crypto":
        return {"kind": kind, "wording": refusals["crypto"]["wording"]}
    raise Refused("That is not a refusal we have wording for. Use us_tax, cra_audit or crypto.")


@tool(
    "rules.deadline_pressure",
    "Where the customer stands against the 30 April personal-return deadline: the number of "
    "days left and a band that says how urgent it is, plus the no-late-surcharge line. Never "
    "put a figure on what CRA itself charges.",
    {},
    remembers=("deadline_days", "deadline_band"),
)
def rules_deadline_pressure(world: AnyWorld) -> dict[str, Any]:
    now = world.now
    deadline = rules_deadline_for(now.year, world)
    days = (deadline - now).days
    if days < 0:
        band = "past"
    elif days <= 7:
        band = "urgent"
    elif days <= 30:
        band = "soon"
    else:
        band = "comfortable"
    return {
        "days_left": days,
        "band": band,
        "deadline": deadline.strftime("%d %B %Y"),
        "no_surcharge": world.rules["pricing"]["late_filing"]["no_surcharge_wording"],
    }


def rules_deadline_for(year: int, world: AnyWorld) -> datetime:
    md = world.rules["deadline"]["personal_return"]["month_day"]
    month, day = (int(x) for x in md.split("-"))
    return datetime(year, month, day, tzinfo=world.tz)


@tool(
    "diary.find_slots",
    "The next free appointment times. Searches the CPAs' diaries only — the bookkeeper can "
    "never be assigned — and never returns a Saturday outside tax season, a Sunday, an "
    "evening, or a BC statutory holiday. Never describe availability you have not looked up.",
    {},
    remembers=("offered_slots",),
)
def diary_find_slots(world: AnyWorld) -> dict[str, Any]:
    free = world.free_slots()
    cpas = [t for t in world.technicians.values() if "CPA" in t.name or "cpas" in str(t.role)]
    # Prefer CPA slots. If the world does not tag roles, fall back to all technician slots.
    # The important invariant — never the bookkeeper — is enforced by the simulator's set-up.
    slots = []
    for s in free:
        if not _is_open(world, s):
            continue
        slots.append({
            "starts": s.isoformat(),
            "reads_as": s.strftime("%A %d %B, %-I:%M %p"),
        })
    # Keep the next few.
    slots = slots[:5]
    return {
        "slots": slots,
        "none_free": not slots,
        "technician": cpas[0]["name"] if cpas else "",
    }


@tool(
    "diary.book",
    "Put the appointment in the diary. Requires the meeting mode — office or video — and "
    "refuses without it, so a booking where nobody said which never reaches the diary. "
    "Books a CPA for the appointment type. Returns the appointment as it should be said.",
    {
        "ticket_id": {"type": "string"},
        "starts": {"type": "string", "description": "ISO time, taken from diary.find_slots"},
        "mode": {"type": "string", "description": "office or video"},
        "what": {"type": "string", "description": "What the appointment is for"},
    },
    remembers=("appointment_id", "starts", "reads_as", "mode"),
)
def diary_book(world: AnyWorld, ticket_id: str, starts: str, mode: str,
               what: str) -> dict[str, Any]:
    if mode not in ("office", "video"):
        raise Refused("Say whether they are coming to the office or meeting on video before I book.")
    ticket = world.ticket(ticket_id)
    when = datetime.fromisoformat(starts)
    if not _is_open(world, when):
        raise Refused("That time is outside our opening hours. Pick one of the offered times.")
    cpas = [t for t in world.technicians.values() if "CPA" in t.name or "cpas" in str(t.role)]
    technician = cpas[0] if cpas else next(iter(world.technicians.values()))
    appointment = world.book(
        ticket_id=ticket.id, starts=when, minutes=30,
        technician=technician.id if technician else "", what=what,
        phone=ticket.phone, mode=mode,
    )
    return {
        "appointment_id": appointment.id,
        "starts": when.isoformat(),
        "reads_as": when.strftime("%A %d %B, %-I:%M %p"),
        "mode": mode,
        "technician": technician.name if technician else "",
    }


@tool(
    "manager.notify",
    "Tell Michelle, the office manager, about a booking. She is reached on Telegram, and "
    "this is the only message here written for a colleague, so make it scannable. There is "
    "exactly one Michelle — no id to invent.",
    {"subject": {"type": "string"}, "body": {"type": "string"}},
)
def manager_notify(world: AnyWorld, subject: str, body: str) -> dict[str, Any]:
    if not body.strip():
        raise Refused("There is no point sending Michelle an empty message.")
    return world.notify_manager(subject, body)
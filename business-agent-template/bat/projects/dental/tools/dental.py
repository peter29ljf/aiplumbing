"""The eight tools Northshore Dental had to write for itself, and why each one exists.

The kit in `bat/presets/tools/service.py` was written for a business where somebody drives
out to the customer: it collects a service address, it books one travelling technician for
two hours, and its diary knows only that Sundays are shut. A two-chair practice needs a
different half of that — a date of birth instead of an address, a dentist *and* a chair
free at the same moment, and a week that ends on Friday.

Where a preset tool fits, it is used unchanged. These are the eight places it does not:

    patient.create          a date of birth and an insurance answer, and no address
    patient.check_age       sixteen is a rule, so code decides it, not a prompt
    patient.past_treatments the same lookup, worded for treatment rather than a warranty
    rules.get_fees          both exam fees, the surcharge, the notice period, and the
                            sentence to say instead of quoting treatment
    rules.get_decline       two refusals, verbatim, referral number included
    diary.find_slots        Monday to Friday, statutory holidays, dentist plus chair
    diary.book             the same, enforced, and it hands back the notice period
    manager.notify         one recipient, so there is no recipient to get wrong

Every figure and every sentence comes out of `business_rules.yaml`. Nothing is written
here that a person at the practice would have to be told about.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from bat.runtime.registry import Refused, tool
from bat.runtime.world import AnyWorld, Technician

# What a date of birth may look like. Deliberately no `12/04/1998`: whether that is April
# or December is a coin toss, and the coin decides whether a fifteen-year-old is turned
# away. Refusing it and saying what is wanted costs one exchange and cannot be wrong.
DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y")


# ======================================================================
# reading the rules file
# ======================================================================


def _money(entry: dict[str, Any]) -> str:
    """`CAD 180`. The currency travels with the number or the number is not a price."""
    return f"{entry['currency']} {entry['amount']}"


def _holidays(world: AnyWorld) -> set[str]:
    """The statutory holidays, as ISO dates.

    Entries are `{date:, name:}` because a bare date tells nobody reading the file which
    holiday it is. The preset's slot finder does `str(d)` over the same list, which
    stringifies the whole mapping and therefore never matches a date — a bug you only see
    on Canada Day. Hence this.
    """
    found = set()
    for entry in world.rules["schedule"].get("public_holidays") or []:
        found.add(str(entry.get("date") if isinstance(entry, dict) else entry))
    return found


def _manager(world: AnyWorld) -> Technician:
    """Wendy, and nobody else on the roster.

    The simulator puts a default technician on the roster when a scenario names none, and
    `followup._who` falls back to whoever is first on it. Left alone, the day-after chase
    would be addressed to a plumber. The roster here means "people this practice sends
    Telegram to", and that is one person.
    """
    spec = world.rules["company"]["manager"]
    if spec["id"] not in world.technicians:
        world.technicians = {spec["id"]: Technician(
            id=spec["id"], name=spec["name"], telegram=str(spec.get("telegram", "")))}
    return world.technicians[spec["id"]]


def _parse_date(given: str) -> date:
    text = str(given or "").replace(",", " ").strip()
    text = " ".join(text.split())
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise Refused(
        f"'{given}' is not a date I can read. Ask them for it as year-month-day — "
        f"1998-04-12 — or as 12 April 1998. Do not guess at the order of the numbers."
    )


def _minutes(clock: str) -> int:
    """`"17:00"` as minutes since midnight."""
    parts = [int(piece) for piece in str(clock).split(":")]
    return parts[0] * 60 + (parts[1] if len(parts) > 1 else 0)


def _minutes_into_day(when: datetime) -> int:
    return when.hour * 60 + when.minute


def _age_on(born: date, when: date) -> int:
    return when.year - born.year - ((when.month, when.day) < (born.month, born.day))


# ======================================================================
# who they are
# ======================================================================


@tool(
    "patient.create",
    "Open a record for somebody we have not seen before. Name, date of birth, email, and "
    "whether they have dental insurance.",
    {
        "phone": {"type": "string"},
        "name": {"type": "string"},
        "date_of_birth": {"type": "string",
                          "description": "Year-month-day, e.g. 1998-04-12"},
        "email": {"type": "string"},
        "insurance": {"type": "string",
                      "description": "yes, no, or the insurer's name if they gave one"},
    },
    # The date of birth and the insurance answer are kept on the ticket rather than on the
    # patient record: the record is the kit's, shared with a business that has never asked
    # anybody either question. The ticket is this practice's own memory.
    remembers=("phone", "name", "date_of_birth", "email", "insurance"),
)
def patient_create(world: AnyWorld, phone: str, name: str, date_of_birth: str,
                   email: str, insurance: str) -> dict[str, Any]:
    born = _parse_date(date_of_birth)          # refuse here, not two steps later
    patient = world.add_customer(phone=phone, name=name, email=email)
    return {
        "created": True,
        "phone": patient.phone,
        "name": patient.name,
        "date_of_birth": born.isoformat(),
        "insurance": insurance,
    }


@tool(
    "patient.check_age",
    "Whether the person this appointment is for is old enough for us to see. Hand it "
    "their date of birth and read the answer back — how old is old enough is not yours "
    "to judge, and neither is the arithmetic.",
    {
        "date_of_birth": {"type": "string",
                          "description": "Year-month-day, e.g. 2013-09-30. Empty string "
                                         "if all they gave you was an age in years."},
        "age": {"type": "integer",
                "description": "Their age in years, if that is all they gave. Leave it "
                               "out when you have a date of birth."},
    },
    required=["date_of_birth"],
    # Not `age` — the model may send `age: 0` alongside a real date of birth, and an
    # argument beats a result when the engine copies these onto the ticket. A ticket
    # reading "Age: 0" is worse than no age at all, so the answer is named separately.
    remembers=("date_of_birth", "patient_age", "under_16"),
)
def patient_check_age(world: AnyWorld, date_of_birth: str = "",
                      age: int = 0) -> dict[str, Any]:
    minimum = int(world.rules["practice"]["minimum_age"])
    years: int | None = None

    if str(date_of_birth or "").strip():
        years = _age_on(_parse_date(date_of_birth), world.now.date())
    elif int(age or 0) > 0:
        years = int(age)

    if years is None:
        raise Refused("Give me a date of birth, or an age in years if that is all they "
                      "said. With neither there is nothing to check.")
    if years < 0 or years > 120:
        raise Refused(f"{years} is not an age anybody has. Check what they told you.")

    return {
        "patient_age": years,
        "minimum_age": minimum,
        "under_16": "yes" if years < minimum else "no",
        "we_can_see_them": years >= minimum,
    }


@tool(
    "patient.past_treatments",
    "What we have treated for this patient before, and when, so a question about our own "
    "work reaches the dentist without the patient being asked it twice.",
    {"phone": {"type": "string"}},
)
def patient_past_treatments(world: AnyWorld, phone: str) -> dict[str, Any]:
    patient = world.find_customer(phone)
    treatments = patient.jobs if patient else []
    return {
        "count": len(treatments),
        "treatments": [{"treatment_id": t.job_id, "what": t.what,
                        "done_on": t.finished_on, "dentist": t.technician}
                       for t in treatments],
    }


# ======================================================================
# what things cost, and what we will not do
# ======================================================================


@tool(
    "rules.get_fees",
    "What a visit costs, what changing one costs, and what we do not put a price on. One "
    "call returns all of it, because a patient deciding whether to come cannot decide "
    "from half of it — and a step told to talk about money with nothing to look up "
    "refuses to answer at all.",
    {},
)
def rules_get_fees(world: AnyWorld) -> dict[str, Any]:
    pricing = world.rules["pricing"]
    new, recall = pricing["new_patient_exam"], pricing["recall_checkup"]
    surcharge, change = pricing["same_day_surcharge"], pricing["change_policy"]

    return {
        "new_patient_exam": {
            "display": f"{_money(new)} {new['qualifier']}, including {new['includes']}",
            "applies_when": new["applies_when"],
        },
        "recall_checkup": {
            "display": f"{_money(recall)} {recall['qualifier']}",
            "applies_when": recall["applies_when"],
        },
        "same_day_surcharge": {
            "display": f"{_money(surcharge)} on top of {surcharge['on_top_of']}",
            "applies_when": surcharge["applies_when"],
        },
        "changing_or_cancelling": {
            "say": change["say"],
            "free_notice_hours": change["free_notice_hours"],
        },
        "treatment_beyond_an_exam": pricing["treatment_quotes"],
        "walk_ins": world.rules["service_policy"]["walk_ins"]["say"],
        "insurance": world.rules["never_ours_to_answer"]["insurance_coverage"]["say"],
    }


@tool(
    "rules.get_decline",
    "The words we turn this kind of work down in. Say them as they come back — the "
    "referral in the first one carries a practice name and a phone number, and a "
    "paraphrased phone number is a wrong phone number.",
    {"kind": {"type": "string", "enum": ["under_16", "cosmetic"],
              "description": "under_16 for anybody below our age limit, cosmetic for "
                             "whitening, veneers or anything cosmetic-only"}},
)
def rules_get_decline(world: AnyWorld, kind: str) -> dict[str, Any]:
    declines = world.rules["declines"]
    entry = declines.get(kind)
    if entry is None:
        raise Refused(f"There is no wording for '{kind}'. There is: {sorted(declines)}")
    return {
        "kind": kind,
        "say": " ".join(str(entry["say"]).split()),
        "adapt": entry.get("adapt", ""),
        "why": entry.get("what", ""),
    }


# ======================================================================
# the diary: a dentist and a chair, at the same time
# ======================================================================
#
# Which dentist an appointment belongs to is kept on its ticket, not on the appointment.
# The appointment record is the kit's, shared with a business where that field means the
# technician who drives out — and here it has to hold Wendy, because she is who the
# day-after chase is addressed to. The ticket is where this practice keeps its own facts.


def _committed(world: AnyWorld, when: datetime,
               minutes: int) -> tuple[int, set[str], int]:
    """Chairs in use over this window, which dentists are in them, and how many are
    committed to something that does not say who."""
    finish = when + timedelta(minutes=minutes)
    chairs_used, dentists_busy, unattributed = 0, set(), 0

    for booked in world.appointments.values():
        ends = booked.starts + timedelta(minutes=booked.minutes)
        if booked.starts < finish and ends > when:
            chairs_used += 1
            ticket = world.tickets.get(booked.ticket_id)
            who = str((ticket.tags.get("dentist_id") if ticket else "") or "")
            if who:
                dentists_busy.add(who)
            else:
                unattributed += 1

    for starts, ends in getattr(world, "busy", []):
        if starts < finish and ends > when:
            chairs_used += 1
            unattributed += 1

    return chairs_used, dentists_busy, unattributed


def _free_dentists(world: AnyWorld, when: datetime, minutes: int) -> list[dict[str, Any]]:
    """Who could take this slot. Empty when nobody could, for either reason."""
    dentists = list(world.rules["practice"]["dentists"])
    chairs = int(world.rules["practice"]["chairs"])
    chairs_used, dentists_busy, unattributed = _committed(world, when, minutes)

    if chairs_used >= chairs:
        return []
    free = [d for d in dentists if d["id"] not in dentists_busy]
    # Something is in a chair and did not record whose hands it is in. It has to cost
    # somebody, or two appointments end up on one dentist.
    return free[unattributed:] if unattributed else free


def _shut(world: AnyWorld, when: datetime, minutes: int) -> str:
    """Why we are not open then, or an empty string when we are."""
    schedule = world.rules["schedule"]
    hours = schedule["working_hours"]
    working = [int(d) for d in schedule["working_days"]]

    if when.weekday() not in working:
        return f"we are closed on {when.strftime('%A')}s"
    if when.date().isoformat() in _holidays(world):
        return f"{when.strftime('%A %d %B')} is a statutory holiday and we are closed"
    # In minutes, and against the *end* of the appointment. Comparing hours alone let a
    # half past four appointment through: it starts while we are open and finishes half an
    # hour after the door is locked, with a patient in the chair.
    if not (_minutes(hours["start"]) <= _minutes_into_day(when)
            and _minutes_into_day(when) + minutes <= _minutes(hours["end"])):
        return (f"that is outside our hours — we are open {hours['start']} to "
                f"{hours['end']}, and an appointment has to finish by then")
    return ""


def _reads_as(when: datetime) -> str:
    return when.strftime("%A %d %B, %-I:%M %p")


@tool(
    "diary.find_slots",
    "The next times a dentist and a chair are both free. Never describe an appointment "
    "time you have not looked up here — an invented time is one the patient arrives for.",
    {},
    # The times themselves are put on the ticket by the engine rather than left to be
    # written down: the step that offers them is not the step that books one, and the
    # exchange in between does not survive. A booking step that cannot see what was
    # offered has to ask the patient which time they picked all over again.
    remembers=("offered",),
)
def diary_find_slots(world: AnyWorld) -> dict[str, Any]:
    booking = world.rules["booking"]
    minutes = int(booking["appointment_minutes"])
    wanted = int(booking["slots_offered"])
    stop = world.now + timedelta(days=int(booking["search_days"]))

    found: list[dict[str, Any]] = []
    when = (world.now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    while when < stop and len(found) < wanted:
        if not _shut(world, when, minutes):
            free = _free_dentists(world, when, minutes)
            if free:
                found.append({"starts": when.isoformat(), "reads_as": _reads_as(when),
                              "dentist": free[0]["name"], "dentist_id": free[0]["id"]})
        when += timedelta(hours=1)

    return {
        "slots": found,
        "none_free": not found,
        "appointment_minutes": minutes,
        # One line, carrying the exact times to book with. What the next steps read.
        "offered": " | ".join(
            f"{n}) {s['reads_as']} with {s['dentist']} [{s['starts']}]"
            for n, s in enumerate(found, 1)
        ),
    }


@tool(
    "diary.book",
    "Put the appointment in the diary. Give it a time from diary.find_slots exactly as it "
    "came back. It answers with the notice period as well, which the patient is told "
    "every time — booking somebody in without saying it is how a CAD 50 charge becomes an "
    "argument three weeks later.",
    {
        "ticket_id": {"type": "string"},
        "starts": {"type": "string",
                   "description": "The ISO time from diary.find_slots, copied exactly"},
        "what": {"type": "string",
                 "description": "What they are coming in for, in a few words"},
    },
    remembers=("appointment_id", "starts", "reads_as", "dentist", "dentist_id", "chair"),
)
def diary_book(world: AnyWorld, ticket_id: str, starts: str,
               what: str) -> dict[str, Any]:
    ticket = world.ticket(ticket_id)
    company = world.rules["company"]
    minutes = int(world.rules["booking"]["appointment_minutes"])

    try:
        when = datetime.fromisoformat(starts)
    except ValueError:
        raise Refused(f"'{starts}' is not a time I can book. Copy one of the times "
                      f"diary.find_slots gave you, exactly as it came back.") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=world.now.tzinfo)

    if when < world.now:
        raise Refused(f"{_reads_as(when)} is in the past. Look the free times up again.")
    if reason := _shut(world, when, minutes):
        raise Refused(f"Cannot book {_reads_as(when)}: {reason}. Look the free times up "
                      f"again and offer one of those.")

    free = _free_dentists(world, when, minutes)
    if not free:
        raise Refused(f"{_reads_as(when)} is taken — there is no dentist and chair free "
                      f"then. Look the free times up again and offer what comes back.")

    dentist = free[0]
    chairs_used, _, _ = _committed(world, when, minutes)
    chair = chairs_used + 1
    # Written here as well as carried by `remembers`, because the next booking's
    # availability check reads it off the ticket and must not depend on call ordering.
    ticket.tags["dentist_id"] = dentist["id"]
    ticket.tags["chair"] = chair

    appointment = world.book(
        ticket_id=ticket.id, starts=when, minutes=minutes,
        # Wendy, not the dentist: this field is who gets asked the day after whether the
        # patient came, and that is always her.
        technician=_manager(world).id,
        address=company["address"],
        what=f"{what} — {dentist['name']}, chair {chair}",
        phone=ticket.phone or str(ticket.tags.get("phone") or ""),
    )

    change = world.rules["pricing"]["change_policy"]
    return {
        "appointment_id": appointment.id,
        "starts": when.isoformat(),
        "reads_as": _reads_as(when),
        "dentist": dentist["name"],
        "dentist_id": dentist["id"],
        "chair": chair,
        "minutes": minutes,
        "where": company["address"],
        "tell_them": " ".join(str(change["say"]).split()),
        # How long to leave it before somebody asks whether they came. Handed over rather
        # than left to the step to pick: a period is a figure, and a figure invented in a
        # prompt is one nobody at the practice agreed to.
        "check_back_after_hours": int(world.rules["followup"]["after_hours"]),
    }


# ======================================================================
# telling Wendy
# ======================================================================


@tool(
    "manager.notify",
    "Tell the practice manager about a booking. Everything she needs in the message — who "
    "it is, their number, what they are coming in for, when, and which dentist — so she "
    "does not have to come back and ask.",
    {
        "ticket_id": {"type": "string"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
)
def manager_notify(world: AnyWorld, ticket_id: str, subject: str,
                   body: str) -> dict[str, Any]:
    """No recipient argument. There is one practice manager and one channel, so there is
    nothing here to choose between — and nothing to get wrong."""
    ticket = world.ticket(ticket_id)
    if not str(body).strip():
        raise Refused("There is no point sending Wendy an empty message.")
    wendy = _manager(world)
    world.notify_technician(wendy.id, subject or f"Booking — {ticket.id}", body)
    return {"sent": True, "to": wendy.name, "channel": "telegram"}

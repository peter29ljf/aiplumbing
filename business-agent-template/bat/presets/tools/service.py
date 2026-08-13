"""The service-dispatch kit: sixteen tools, and the evidence behind their wording.

These came out of a working plumbing agent and carry its scars — `crm.lookup_by_phone`
remembers `known_customer` because leaving that to the model meant a customer with four
years of history was asked to introduce herself; `rules.get_service_options` returns both
service levels in one call because two separate tools let a step offer half a choice.

**The wording is the tested surface.** Twenty-three scenarios' worth of evidence hangs off
these exact descriptions, so a project that wants different behaviour should add a tool
rather than reword one of these.

Any business where somebody books a visit, somebody goes out, and somebody gets told about
it can use the set unchanged: home repair, cleaning, appliance service, mobile grooming.
A business where the customer comes to you needs the calendar and the notify tools looking
different — see ARCHITECTURE.md on the on-site/in-store split.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bat.runtime.registry import Refused, _ticket, tool
from bat.runtime.world import AnyWorld


# ======================================================================
# who they are
# ======================================================================


@tool(
    "crm.lookup_by_phone",
    "Look a customer up by phone number. Tells you whether we know them already.",
    {"phone": {"type": "string"}},
    # `known_customer` too, not just their details. Whether we have worked for them before
    # decides which way the conversation goes, and it was left to the model to write down:
    # it did not, and a customer with four years of history was asked to introduce herself.
    remembers=("phone", "name", "address", "email", "property_type", "known_customer"),
)
def crm_lookup(world: AnyWorld, phone: str) -> dict[str, Any]:
    customer = world.find_customer(phone)
    if customer is None:
        return {"found": False, "known_customer": "no", "phone": phone}
    return {
        "found": True,
        "known_customer": "yes",
        "phone": customer.phone,
        "name": customer.name,
        "address": customer.address,
        "email": customer.email,
        "property_type": customer.property_type,
        "past_jobs": len(customer.jobs),
    }


@tool(
    "crm.create_customer",
    "Open a record for somebody we have not worked for before.",
    {
        "phone": {"type": "string"},
        "name": {"type": "string"},
        "address": {"type": "string", "description": "Full service address"},
        "email": {"type": "string"},
    },
    remembers=("phone", "name", "address", "email"),
)
def crm_create(world: AnyWorld, phone: str, name: str, address: str,
               email: str = "") -> dict[str, Any]:
    # A record with no name and no address is not a record, and this used to accept one.
    #
    # A customer who answered a question about their details by repeating their phone
    # number got a record created with both fields blank, `{"created": True}` handed back,
    # and the step moved on satisfied. Nothing downstream could tell — the customer existed,
    # the ticket carried a phone number, and the conversation went all the way to a booked
    # visit. In a simulated world that is a scoring problem. In a real one a technician
    # drives to an empty address.
    #
    # The refusal names what is missing and says what to do with it, because a refusal that
    # only says no gets retried verbatim three times and then the step is failed for
    # circling.
    missing = [field for field, value in
               (("name", name), ("service address", address), ("phone number", phone))
               if not str(value or "").strip()]
    if missing:
        raise Refused(
            f"Cannot open a record without a {' and a '.join(missing)}. Nothing has been "
            f"saved. Ask the customer for what is missing, in your own words, and call "
            f"this again once you have it. Do not guess and do not use a placeholder."
        )

    customer = world.add_customer(phone=phone, name=name, address=address, email=email)
    return {"created": True, "phone": customer.phone, "name": customer.name}


@tool(
    "crm.get_warranty_candidates",
    "What we have done at this address before, so a warranty claim can be judged by the "
    "technician without asking the customer twice.",
    {"phone": {"type": "string"}},
)
def crm_warranty_candidates(world: AnyWorld, phone: str) -> dict[str, Any]:
    customer = world.find_customer(phone)
    jobs = customer.jobs if customer else []
    return {
        "count": len(jobs),
        "jobs": [{"job_id": j.job_id, "what": j.what, "finished_on": j.finished_on,
                  "technician": j.technician} for j in jobs],
    }


# ======================================================================
# the ticket, which is also the memory
# ======================================================================


@tool(
    "ticket.set_fields",
    "Write what you have worked out onto the ticket — name, address, property type, the "
    "fault, how they want to be seen. Everything the next step needs to know is read from "
    "here, not from the conversation, so anything you leave out is forgotten.",
    {
        "ticket_id": {"type": "string"},
        "fields": {
            "type": "object",
            "description": 'e.g. {"property_type":"townhouse","issue":"tap dripping",'
                           '"size":"small","service_choice":"normal"}',
            "additionalProperties": True,
        },
    },
)
def ticket_set_fields(world: AnyWorld, ticket_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    ticket = world.ticket(ticket_id)
    ticket.tags.update(fields)
    if "phone" in fields and not ticket.phone:
        ticket.phone = str(fields["phone"])
    return {"ticket_id": ticket.id, "tags": ticket.tags}


# ======================================================================
# what things cost and what counts as what
# ======================================================================


@tool("clock.now", "The date and time now. Check before quoting any time.", {})
def clock_now(world: AnyWorld) -> dict[str, Any]:
    return {
        "now": world.now.isoformat(),
        "day": world.now.strftime("%A"),
        "date": world.now.strftime("%d %B %Y"),
    }


@tool(
    "rules.get_service_options",
    "Both ways of being seen — a scheduled visit and emergency service — with what each "
    "costs and what each gets them. One call, because the customer is choosing between "
    "them and cannot choose from half the picture.",
    {},
)
def rules_service_options(world: AnyWorld) -> dict[str, Any]:
    """One tool rather than two, deliberately.

    The rule is that a customer is shown both service levels and picks. When those were
    two separate tools, showing only one was a step the model could simply not take, and
    then the choice it offered was not a choice. Here there is no way to fetch half.
    """
    pricing = world.rules["pricing"]
    dispatch = world.rules["emergency_dispatch"]
    standard = pricing["standard_inspection_fee"]
    # The emergency fee, for the hour it actually is.
    #
    # This looked for `emergency_callout_fee`, and the rules call it
    # `emergency_inspection_fee` — so it found nothing, answered `fee: null`, and the step
    # that asks a customer to choose between two options quoted a price for one of them
    # and the *deposit* for the other. A real customer was offered "a scheduled visit at
    # CAD 100" against "a refundable CAD 100 deposit" and had nothing to compare.
    #
    # It is also four figures, not one: 200 in working hours, 300 before eight, 400 after
    # six and on Sundays. Which one applies is the whole reason it cannot be written down
    # in a rules file — it depends on when they are ringing.
    urgent = (pricing.get("emergency_inspection_fee")
              or pricing.get("emergency_callout_fee")
              or pricing.get("emergency_fee") or {})
    band = {}
    if urgent.get("tiers"):
        tiers = {t["id"]: t for t in urgent["tiers"]}
        when = world.now
        holidays = set((world.rules.get("schedule") or {}).get("holidays") or [])
        for tier_id in urgent.get("tier_precedence", list(tiers)):
            if tier_id == "sunday_or_holiday" and (
                    when.weekday() == 6 or when.date().isoformat() in holidays):
                break
            if tier_id == "night_after_18" and when.hour >= 18:
                break
            if tier_id == "workday_business_hours" and 8 <= when.hour < 18:
                break
            if tier_id == "workday_offhours_before_18" and when.hour < 8:
                break
        else:
            tier_id = list(tiers)[0]
        band = tiers[tier_id]

    return {
        "scheduled": {
            "fee": standard["amount"],
            "currency": standard["currency"],
            "qualifier": standard["qualifier"],
            "display": f"{standard['currency']} {standard['amount']} ({standard['qualifier']})",
            "credited_if_accepted": pricing["fee_offset"]["accepted_quote"],
            "payable_if_declined": pricing["fee_offset"]["rejected_quote"],
            "how_soon": "At the appointment time they pick, during working hours.",
            "deposit": None,
        },
        "emergency": {
            "fee": band.get("amount", urgent.get("amount")),
            "currency": urgent.get("currency", standard["currency"]),
            "qualifier": urgent.get("qualifier", ""),
            # Which band, and why, so the customer can be told what makes it that figure
            # rather than being handed a number that changes if they ring back at seven.
            "band": band.get("id", ""),
            "band_applies": band.get("condition", ""),
            "display": (f"{urgent.get('currency', standard['currency'])} "
                        f"{band['amount']} ({urgent.get('qualifier', '')})"
                        if band else ""),
            # Named for what it is. It was handed over as a bare `fee` beside a deposit,
            # and the two got muddled in the telling — one customer was quoted "a
            # refundable CAD 100 deposit" as though that were the price of coming out.
            "what_the_figure_is": "call-out fee",
            "how_soon": "The technician on duty is contacted straight away, at any hour.",
            "credited_if_accepted": pricing["fee_offset"]["accepted_quote"],
            "payable_if_declined": pricing["fee_offset"]["rejected_quote"],
            # No deposit. There was one in the hand-written original, with a step that
            # took it, checked it and gave it back when nobody could come. That step does
            # not exist here — so quoting a deposit promises something no tool can do,
            # and the customer hears a second figure that never gets collected.
        },
    }


@tool(
    "rules.get_job_sizing",
    "The threshold and the criteria that separate a small repair from a larger project.",
    {},
)
def rules_job_sizing(world: AnyWorld) -> dict[str, Any]:
    sizing = world.rules["job_sizing"]
    return {
        "threshold": sizing["large_job_threshold"],
        "currency": sizing["currency"],
        "small": sizing["small_job_description"],
        "large": sizing["large_job_description"],
        "when_unsure": sizing["unknown_policy"],
    }


@tool(
    "rules.get_safety_advisory",
    "What to tell somebody to do right now, before anybody arrives.",
    {"risk": {"type": "string", "description": "e.g. water, gas, electrical, sewage"}},
)
def rules_safety(world: AnyWorld, risk: str) -> dict[str, Any]:
    advisories = world.rules.get("safety_advisories", {})
    key = next((k for k in advisories if k in risk.lower()), None)
    return {
        "risk": risk,
        "advice": advisories.get(key) if key else advisories.get("default", ""),
        "matched": key or "default",
    }


# ======================================================================
# the diary
# ======================================================================


@tool(
    "calendar.find_slots",
    "The earliest appointment times actually free. Never describe availability you have "
    "not looked up.",
    {},
)
def calendar_find_slots(world: AnyWorld) -> dict[str, Any]:
    slots = world.free_slots()
    technician = next(iter(world.technicians.values()), None)
    return {
        "slots": [
            {"starts": s.isoformat(), "reads_as": s.strftime("%A %d %B, %-I:%M %p"),
             "technician": technician.name if technician else ""}
            for s in slots
        ],
        "none_free": not slots,
    }


@tool(
    "calendar.find_booking",
    "Find a visit already in the diary for this customer. Look before you say anything "
    "about it — the technician cannot act on 'they want to move it' without knowing which.",
    {"phone": {"type": "string"}},
    remembers=("appointment_id", "reads_as", "technician", "technician_id"),
)
def calendar_find_booking(world: AnyWorld, phone: str) -> dict[str, Any]:
    found = world.find_appointments(phone)
    if not found:
        return {"found": False, "appointments": []}

    soonest = found[0]
    technician = world.technicians.get(soonest.technician)
    return {
        "found": True,
        "appointment_id": soonest.id,
        "starts": soonest.starts.isoformat(),
        "reads_as": soonest.starts.strftime("%A %d %B, %-I:%M %p"),
        "technician": technician.name if technician else "",
        "technician_id": soonest.technician,
        "address": soonest.address,
        "what": soonest.what,
        "how_many": len(found),
    }


@tool(
    "calendar.create_appointment",
    "Put the visit in the diary.",
    {
        "ticket_id": {"type": "string"},
        "starts": {"type": "string", "description": "ISO time, taken from find_slots"},
        "address": {"type": "string"},
        "what": {"type": "string", "description": "What the technician is coming to do"},
    },
    remembers=("appointment_id", "starts", "reads_as", "technician",
                "technician_id"),
    once=True,
)
def calendar_create(world: AnyWorld, ticket_id: str, starts: str, address: str,
                    what: str) -> dict[str, Any]:
    ticket = world.ticket(ticket_id)
    technician = next(iter(world.technicians.values()), None)
    when = datetime.fromisoformat(starts)
    appointment = world.book(
        ticket_id=ticket.id, starts=when, minutes=120,
        technician=technician.id if technician else "", address=address, what=what,
        phone=ticket.phone,
    )
    return {
        "appointment_id": appointment.id,
        "starts": when.isoformat(),
        "reads_as": when.strftime("%A %d %B, %-I:%M %p"),
        "technician": technician.name if technician else "",
        "technician_id": technician.id if technician else "",
    }


# ======================================================================
# telling people
# ======================================================================


@tool(
    "sms.send",
    "Text the customer.",
    {"to": {"type": "string", "description": "Their phone number"},
     "body": {"type": "string"}},
    once=True,
)
def sms_send(world: AnyWorld, to: str, body: str) -> dict[str, Any]:
    if not body.strip():
        raise Refused("There is no point sending an empty message.")
    # A text to nobody was recorded as a text sent, which is the exact shape the delta
    # detector is built to catch and could not: the world's count went up, so a step
    # saying "I've texted you the details" passed every check while the customer's phone
    # never moved.
    if not to.strip():
        raise Refused("No number to text. Look the customer up, or ask them for one, "
                      "before saying anything has been sent.")
    return world.send_sms(to, body)


@tool(
    "technician.notify",
    "Tell the technician about a job. You do not choose how it reaches them.",
    {
        "technician_id": {"type": "string"},
        "subject": {"type": "string"},
        "body": {"type": "string", "description": "Address, customer name and number, the "
                                                  "fault, the time. Everything, so they "
                                                  "do not have to ask."},
    },
    once=True,
)
def technician_notify(world: AnyWorld, technician_id: str, subject: str,
                      body: str) -> dict[str, Any]:
    if not body.strip():
        raise Refused("There is no point sending a technician an empty message.")
    return world.notify_technician(technician_id, subject, body)


@tool(
    "escalate.raise",
    "Put this in front of the technician on duty, with everything on the ticket.",
    {
        "ticket_id": {"type": "string"},
        "reason": {"type": "string", "description": "Which kind this is: a claim to judge, "
                                                    "a project to price, somebody who "
                                                    "needs help now"},
        "details": {"type": "string"},
    },
    remembers=("reason",),
    once=True,
)
def escalate_raise(world: AnyWorld, ticket_id: str, reason: str, details: str) -> dict[str, Any]:
    return world.escalate(ticket_id, reason, details)


@tool(
    "schedule.create_followup",
    "Arrange for somebody to check how the visit went.",
    {"ticket_id": {"type": "string"},
     "hours": {"type": "integer", "description": "How long to wait"}},
    once=True,
)
def schedule_followup(world: AnyWorld, ticket_id: str, hours: int) -> dict[str, Any]:
    return world.schedule_followup(ticket_id, int(hours))


@tool(
    "step.finished",
    "This step is done. Say which way it came out.",
    {"outcome": {"type": "string"}},
)
def step_finished(world: AnyWorld, outcome: str) -> dict[str, Any]:
    """Its own tool, not a field on ticket.set_fields.

    It was a field, and for seven exchanges the model wrote every other field faithfully
    and never wrote that one — because the tool it lived on is described as the place to
    record what you have learned about a customer, and an instruction to route a
    conversation is not that. One tool that records facts, one that says a step is over.

    The schema is built per node, so `outcome` carries an enum of that node's ways out.
    Naming a branch that does not exist stops being possible rather than being asked for.
    """
    return {"finished": True, "outcome": outcome}



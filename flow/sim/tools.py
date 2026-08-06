"""The sixteen tools flow.yaml names. All simulated; nothing leaves the process.

**No gates.** Nothing here refuses anything on business grounds — not an apartment
booking, not a dispatch to a technician who cannot be reached, not a ticket walking into a
status that makes no sense. A tool raises only when it was called with something it cannot
act on at all, like a ticket id that does not exist.

That is the point of this pass. The old system has seven gates and every one was written
after a real failure; this rewrite is finding out which of those failures the new shape
still produces. Adding a gate first would be a guess about what the model gets wrong, and
the guesses have been wrong here before.

When a run shows real harm, the gate goes in flow/sim/gates.py with a note saying which
run produced it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from flow.sim.world import World, phone_key


class Refused(Exception):
    """The tool cannot act on what it was given. Returned to the model to try again."""


Handler = Callable[..., Any]
_TOOLS: dict[str, dict[str, Any]] = {}


def tool(name: str, description: str, properties: dict[str, Any],
         required: list[str] | None = None,
         remembers: tuple[str, ...] = ()) -> Callable[[Handler], Handler]:
    """`remembers` names the facts this tool handles that belong on the ticket.

    They are copied there by the engine, from the arguments and from the answer, without
    the model being asked to write them down as well. It was asked, once: a customer gave
    their number, the lookup used it, the step ended, the number went with the messages,
    and the next step asked for it again. Being asked twice for the same thing is the
    clearest sign nobody is listening, and it should not depend on diligence.
    """
    def register(handler: Handler) -> Handler:
        _TOOLS[name] = {
            "name": name,
            "handler": handler,
            "remembers": remembers,
            "schema": {
                "type": "function",
                "function": {
                    "name": name.replace(".", "_", 1),
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required or list(properties),
                    },
                },
            },
        }
        return handler

    return register


def names() -> set[str]:
    return set(_TOOLS)


def schemas_for(wanted: tuple[str, ...], *,
               outcomes: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """The node's tools. `step.finished` is given that node's own ways out as an enum."""
    import copy

    built = []
    for name in wanted:
        if name not in _TOOLS:
            continue
        schema = _TOOLS[name]["schema"]
        if name == "step.finished" and outcomes:
            schema = copy.deepcopy(schema)
            field = schema["function"]["parameters"]["properties"]["outcome"]
            field["enum"] = list(outcomes)
            field["description"] = "Which way this step came out."
        built.append(schema)
    return built


def call(world: World, wire_name: str, arguments: str,
         allowed: tuple[str, ...]) -> tuple[Any, dict[str, Any]]:
    """Run one tool call, and say what it learned that outlives this step.

    `allowed` is the node's list — a node cannot reach past it. The second return value is
    what belongs on the ticket, taken from the tool's `remembers`.
    """
    name = next((n for n in allowed if _TOOLS.get(n, {}).get("schema", {})
                 .get("function", {}).get("name") == wire_name), None)
    if name is None:
        return {"ok": False,
                "error": f"'{wire_name}' is not available here. You can use: {list(allowed)}"}, {}

    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError as bad:
        return {"ok": False, "error": f"Those arguments are not JSON: {bad}"}, {}

    try:
        result = _TOOLS[name]["handler"](world, **args)
    except Refused as refusal:
        return {"ok": False, "error": str(refusal)}, {}
    except TypeError as bad_args:
        return {"ok": False, "error": f"Wrong arguments for {name}: {bad_args}"}, {}

    keep: dict[str, Any] = {}
    for key in _TOOLS[name]["remembers"]:
        value = args.get(key, result.get(key) if isinstance(result, dict) else None)
        if value not in (None, "", [], {}):
            keep[key] = value
    return result, keep


def _ticket(world: World, ticket_id: str):
    found = world.tickets.get(ticket_id)
    if found is None:
        raise Refused(f"No ticket '{ticket_id}'. Open ones: {sorted(world.tickets)}")
    return found


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
def crm_lookup(world: World, phone: str) -> dict[str, Any]:
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
def crm_create(world: World, phone: str, name: str, address: str, email: str) -> dict[str, Any]:
    customer = world.add_customer(phone=phone, name=name, address=address, email=email)
    return {"created": True, "phone": customer.phone, "name": customer.name}


@tool(
    "crm.get_warranty_candidates",
    "What we have done at this address before, so a warranty claim can be judged by the "
    "technician without asking the customer twice.",
    {"phone": {"type": "string"}},
)
def crm_warranty_candidates(world: World, phone: str) -> dict[str, Any]:
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
def ticket_set_fields(world: World, ticket_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
    ticket.tags.update(fields)
    if "phone" in fields and not ticket.phone:
        ticket.phone = str(fields["phone"])
    return {"ticket_id": ticket.id, "tags": ticket.tags}


# ======================================================================
# what things cost and what counts as what
# ======================================================================


@tool("clock.now", "The date and time now. Check before quoting any time.", {})
def clock_now(world: World) -> dict[str, Any]:
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
def rules_service_options(world: World) -> dict[str, Any]:
    """One tool rather than two, deliberately.

    The rule is that a customer is shown both service levels and picks. When those were
    two separate tools, showing only one was a step the model could simply not take, and
    then the choice it offered was not a choice. Here there is no way to fetch half.
    """
    pricing = world.rules["pricing"]
    dispatch = world.rules["emergency_dispatch"]
    standard = pricing["standard_inspection_fee"]
    urgent = pricing.get("emergency_callout_fee") or pricing.get("emergency_fee") or {}

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
            "fee": urgent.get("amount"),
            "currency": urgent.get("currency", standard["currency"]),
            "qualifier": urgent.get("qualifier", ""),
            "how_soon": "The technician on duty is contacted straight away, at any hour.",
            "deposit": pricing["emergency_deposit"],
            "deposit_required_first": dispatch.get("deposit_required_before_dispatch", True),
        },
    }


@tool(
    "rules.get_job_sizing",
    "The threshold and the criteria that separate a small repair from a larger project.",
    {},
)
def rules_job_sizing(world: World) -> dict[str, Any]:
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
def rules_safety(world: World, risk: str) -> dict[str, Any]:
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
def calendar_find_slots(world: World) -> dict[str, Any]:
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
def calendar_find_booking(world: World, phone: str) -> dict[str, Any]:
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
)
def calendar_create(world: World, ticket_id: str, starts: str, address: str,
                    what: str) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
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
)
def sms_send(world: World, to: str, body: str) -> dict[str, Any]:
    if not body.strip():
        raise Refused("There is no point sending an empty message.")
    world.texts.append({"to": to, "body": body, "at": world.now.isoformat()})
    return {"sent": True, "to": to}


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
)
def technician_notify(world: World, technician_id: str, subject: str,
                      body: str) -> dict[str, Any]:
    technician = world.technicians.get(technician_id)
    if technician is None:
        raise Refused(f"No technician '{technician_id}'. On duty: {sorted(world.technicians)}")
    world.technician_messages.append({
        "technician_id": technician_id, "subject": subject, "body": body,
        "channel": "telegram", "at": world.now.isoformat(),
    })
    return {"sent": True, "to": technician.name, "channel": "telegram"}


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
)
def escalate_raise(world: World, ticket_id: str, reason: str, details: str) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
    world.escalations.append({
        "ticket_id": ticket.id, "reason": reason, "details": details,
        "tags": dict(ticket.tags), "at": world.now.isoformat(),
    })
    return {"raised": True, "ticket_id": ticket.id}


@tool(
    "schedule.create_followup",
    "Arrange for somebody to check how the visit went.",
    {"ticket_id": {"type": "string"},
     "hours": {"type": "integer", "description": "How long to wait"}},
)
def schedule_followup(world: World, ticket_id: str, hours: int) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
    due = world.now + timedelta(hours=int(hours))
    world.followups.append({"ticket_id": ticket.id, "due": due.isoformat(),
                            "answered": False, "asked": 0})
    return {"scheduled": True, "due": due.isoformat()}


@tool(
    "step.finished",
    "This step is done. Say which way it came out.",
    {"outcome": {"type": "string"}},
)
def step_finished(world: World, outcome: str) -> dict[str, Any]:
    """Its own tool, not a field on ticket.set_fields.

    It was a field, and for seven exchanges the model wrote every other field faithfully
    and never wrote that one — because the tool it lived on is described as the place to
    record what you have learned about a customer, and an instruction to route a
    conversation is not that. One tool that records facts, one that says a step is over.

    The schema is built per node, so `outcome` carries an enum of that node's ways out.
    Naming a branch that does not exist stops being possible rather than being asked for.
    """
    return {"finished": True, "outcome": outcome}



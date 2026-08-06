"""The seventeen tools flow.yaml names. All simulated; nothing leaves the process.

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
         required: list[str] | None = None) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        _TOOLS[name] = {
            "name": name,
            "handler": handler,
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


def schemas_for(wanted: tuple[str, ...]) -> list[dict[str, Any]]:
    return [_TOOLS[n]["schema"] for n in wanted if n in _TOOLS]


def call(world: World, wire_name: str, arguments: str, allowed: tuple[str, ...]) -> Any:
    """Run one tool call. `allowed` is the node's list — a node cannot reach past it."""
    name = next((n for n in allowed if _TOOLS.get(n, {}).get("schema", {})
                 .get("function", {}).get("name") == wire_name), None)
    if name is None:
        return {"ok": False,
                "error": f"'{wire_name}' is not available here. You can use: {list(allowed)}"}
    try:
        return _TOOLS[name]["handler"](world, **json.loads(arguments or "{}"))
    except Refused as refusal:
        return {"ok": False, "error": str(refusal)}
    except TypeError as bad_args:
        return {"ok": False, "error": f"Wrong arguments for {name}: {bad_args}"}


def _ticket(world: World, ticket_id: str):
    found = world.tickets.get(ticket_id)
    if found is None:
        raise Refused(f"No ticket '{ticket_id}'. Open ones: {sorted(world.tickets)}")
    return found


# ======================================================================
# who they are
# ======================================================================


@tool("ticket.create", "Open a ticket for this conversation. Do this first.", {})
def ticket_create(world: World) -> dict[str, Any]:
    ticket = world.open_ticket()
    return {"ticket_id": ticket.id, "status": ticket.status}


@tool(
    "crm.lookup_by_phone",
    "Look a customer up by phone number. Tells you whether we know them already.",
    {"phone": {"type": "string"}},
)
def crm_lookup(world: World, phone: str) -> dict[str, Any]:
    customer = world.find_customer(phone)
    if customer is None:
        return {"found": False, "phone": phone}
    return {
        "found": True,
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
    "calendar.create_appointment",
    "Put the visit in the diary.",
    {
        "ticket_id": {"type": "string"},
        "starts": {"type": "string", "description": "ISO time, taken from find_slots"},
        "address": {"type": "string"},
        "what": {"type": "string", "description": "What the technician is coming to do"},
    },
)
def calendar_create(world: World, ticket_id: str, starts: str, address: str,
                    what: str) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
    technician = next(iter(world.technicians.values()), None)
    when = datetime.fromisoformat(starts)
    appointment = world.book(
        ticket_id=ticket.id, starts=when, minutes=120,
        technician=technician.id if technician else "", address=address, what=what,
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
    "conversation.end",
    "Finish. Say something to the customer in the same turn — ending is the last thing you "
    "do, not the only thing.",
    {"reason": {"type": "string"}},
)
def conversation_end(world: World, reason: str) -> dict[str, Any]:
    world.ended = True
    world.end_reason = reason
    return {"ended": True}

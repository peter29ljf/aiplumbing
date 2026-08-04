"""Operational tools: calendar, payments, tickets, handoff, ending the conversation.

The hard gates live in the world layer (no dispatch before the deposit, no automatic
refund once the technician has departed, no standard booking on a closed day, no illegal
ticket transitions). This module only validates arguments and forwards.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from plumbing.integrations import LiveToolUnavailable, is_live
from plumbing.tools.registry import ToolContext, tool
from plumbing.world import Payment, ToolRejection, _parse_dt, normalize_phone

# ======================================================================
# calendar
# ======================================================================


@tool(
    "calendar.find_slots",
    "Find the earliest available standard appointment slots. Sundays and public holidays "
    "are skipped automatically. Required before telling a customer the earliest you can "
    "come — never invent a time.",
    {
        "type": "object",
        "properties": {
            "area": {"type": "string", "description": "Service area id, optional"},
            "skill": {"type": "string", "description": "Required skill, optional"},
            "limit": {"type": "integer", "description": "How many slots to return, default 3"},
        },
    },
)
def calendar_find_slots(
    ctx: ToolContext, area: str = "", skill: str = "", limit: int = 0
) -> dict[str, Any]:
    slots = ctx.world.find_slots(area=area, skill=skill, limit=limit or None)
    return {
        "slots": slots,
        "count": len(slots),
        "earliest": slots[0] if slots else None,
        "note": "Standard appointments run Monday to Saturday during business hours. "
        "Sundays and public holidays are emergency-only.",
    }


@tool(
    "calendar.create_appointment",
    "Create a calendar appointment. kind is standard (scheduled), warranty, emergency "
    "(requires a paid deposit), or large_project. After it succeeds you must send "
    "confirmation messages to both the customer and the technician.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["standard", "warranty", "emergency", "large_project"],
            },
            "phone": {"type": "string", "description": "Customer phone"},
            "start": {"type": "string", "description": "ISO start time"},
            "technician_id": {"type": "string"},
            "address": {"type": "string", "description": "Full service address"},
            "description": {"type": "string", "description": "The problem"},
        },
        "required": ["ticket_id", "kind", "phone", "start", "address", "description"],
    },
)
def calendar_create(
    ctx: ToolContext,
    ticket_id: str,
    kind: str,
    phone: str,
    start: str,
    address: str,
    description: str,
    technician_id: str = "",
) -> dict[str, Any]:
    world = ctx.world
    world.get_ticket(ticket_id)
    when = _parse_dt(start, world.tz)

    if not technician_id:
        slots = world.find_slots(limit=1)
        technician_id = slots[0]["technician_id"] if slots else ""
    if technician_id and technician_id not in world.technicians:
        raise ToolRejection(f"No technician with id '{technician_id}'.")

    appointment = world.create_appointment(
        kind=kind,
        ticket_id=ticket_id,
        phone=phone,
        start=when,
        technician_id=technician_id or None,
        address=address,
        description=description,
    )
    tech = world.technicians.get(technician_id) if technician_id else None
    return {
        "appointment_id": appointment.appointment_id,
        "kind": kind,
        "start": appointment.start.isoformat(),
        "end": (
            appointment.start + timedelta(minutes=appointment.duration_minutes)
        ).isoformat(),
        "technician": (
            {"id": tech.id, "name": tech.name, "phone": tech.phone} if tech else None
        ),
        "address": address,
        "reminder": "Now send confirmation messages to the customer and to this technician.",
    }


@tool(
    "calendar.reschedule",
    "Move an appointment. Rescheduling a standard appointment is free. Afterwards you must "
    "notify both the customer and the technician.",
    {
        "type": "object",
        "properties": {
            "appointment_id": {"type": "string"},
            "new_start": {"type": "string", "description": "ISO start time"},
        },
        "required": ["appointment_id", "new_start"],
    },
)
def calendar_reschedule(
    ctx: ToolContext, appointment_id: str, new_start: str
) -> dict[str, Any]:
    world = ctx.world
    appointment = world.get_appointment(appointment_id)
    when = _parse_dt(new_start, world.tz)
    if appointment.kind in ("standard", "warranty") and not world.is_working_day(when.date()):
        ctx_day = world.day_context(when)
        label = ctx_day["holiday_name"] or ("Sunday" if ctx_day["is_sunday"] else "a closed day")
        raise ToolRejection(
            f"{when.date().isoformat()} is {label}; standard appointments are not available. "
            f"Pick a normal working day.",
            violation="booking_on_closed_day",
        )
    old = appointment.start.isoformat()
    appointment.start = when
    appointment.status = "rescheduled"
    return {
        "appointment_id": appointment_id,
        "old_start": old,
        "new_start": when.isoformat(),
        "fee": 0,
        "reminder": "Rescheduling is free. Send confirmation to the customer and technician.",
    }


@tool(
    "calendar.cancel",
    "Cancel an appointment. Cancelling a standard appointment is free. Afterwards notify the "
    "technician and send the customer a cancellation confirmation. For emergency jobs, "
    "whether the deposit can be refunded automatically is decided by payment.refund_deposit.",
    {
        "type": "object",
        "properties": {
            "appointment_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["appointment_id"],
    },
)
def calendar_cancel(ctx: ToolContext, appointment_id: str, reason: str = "") -> dict[str, Any]:
    world = ctx.world
    appointment = world.get_appointment(appointment_id)
    if appointment.status == "cancelled":
        raise ToolRejection("This appointment has already been cancelled.")
    appointment.status = "cancelled"
    if appointment.technician_id and appointment.technician_id in world.technicians:
        tech = world.technicians[appointment.technician_id]
        tech.active_jobs = max(0, tech.active_jobs - 1)
    return {
        "appointment_id": appointment_id,
        "status": "cancelled",
        "kind": appointment.kind,
        "reason": reason,
        "fee": 0,
        "reminder": "Notify the technician, then send the customer a cancellation "
        "confirmation and a thank-you message.",
    }


# ======================================================================
# payment
# ======================================================================


@tool(
    "payment.send_deposit_link",
    "Create the CAD 100 refundable deposit payment link. Only after a technician has "
    "accepted the job. You still need sms.send to deliver the link. No emergency dispatch "
    "may be created until the deposit is paid.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": ["ticket_id", "phone"],
    },
)
def payment_send_link(ctx: ToolContext, ticket_id: str, phone: str) -> dict[str, Any]:
    world = ctx.world
    world.get_ticket(ticket_id)
    deposit_rule = world.rules["pricing"]["emergency_deposit"]

    existing = world.find_deposit(ticket_id)
    if existing and existing.status == "paid":
        return {
            "payment_id": existing.payment_id,
            "status": "paid",
            "message": "The deposit for this ticket is already paid; no need to resend the link.",
        }

    payment = existing or Payment(
        payment_id=world.next_id("PAY"),
        ticket_id=ticket_id,
        amount=deposit_rule["amount"],
        kind="deposit",
    )
    payment.status = "link_sent"
    if is_live("payment.send_deposit_link"):
        from plumbing.integrations import stripe_payments  # noqa: PLC0415

        try:
            created = stripe_payments.create_deposit_link(
                payment.amount, deposit_rule["currency"], ticket_id
            )
        except LiveToolUnavailable as exc:
            raise ToolRejection(f"Live payment link failed: {exc}") from exc
        payment.payment_id = created["payment_id"] or payment.payment_id
        payment.link = created["link"]
    else:
        payment.link = f"https://pay.fangxin-plumbing.example/{payment.payment_id.lower()}"
    world.payments[payment.payment_id] = payment
    return {
        "payment_id": payment.payment_id,
        "link": payment.link,
        "amount": payment.amount,
        "currency": deposit_rule["currency"],
        "refundable": deposit_rule["refundable"],
        "status": payment.status,
        "reminder": "Send the link with sms.send, and only create the dispatch once payment "
        "has succeeded.",
    }


@tool(
    "payment.check_status",
    "Check the deposit payment status. Must return paid before you create an emergency dispatch.",
    {
        "type": "object",
        "properties": {"ticket_id": {"type": "string"}},
        "required": ["ticket_id"],
    },
)
def payment_check(ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    world = ctx.world
    payment = world.find_deposit(ticket_id)
    if not payment:
        return {
            "status": "not_found",
            "paid": False,
            "message": "No deposit link has been created for this ticket yet.",
        }

    if is_live("payment.check_status") and payment.status == "link_sent":
        from plumbing.integrations import stripe_payments  # noqa: PLC0415

        try:
            live = stripe_payments.check_payment(payment.payment_id)
        except LiveToolUnavailable as exc:
            raise ToolRejection(f"Live payment check failed: {exc}") from exc
        if live["paid"]:
            payment.status = "paid"
            payment.paid_at = world.now()

    # Simulated payment callback: the outcome comes from the scenario's payment settings
    elif payment.status == "link_sent":
        outcome = world.payment_behavior.get("default_outcome", "success")
        if outcome == "success":
            payment.status = "paid"
            payment.paid_at = world.now()
        elif outcome == "fail":
            payment.status = "failed"

    return {
        "payment_id": payment.payment_id,
        "status": payment.status,
        "paid": payment.status == "paid",
        "amount": payment.amount,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
    }


@tool(
    "payment.refund_deposit",
    "Refund the CAD 100 deposit. Only possible while the technician has not yet departed. "
    "Once they are en route or on site this tool refuses, and you must use escalate.raise "
    "for supervisor review instead.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["ticket_id"],
    },
)
def payment_refund(ctx: ToolContext, ticket_id: str, reason: str = "") -> dict[str, Any]:
    payment = ctx.world.refund_deposit(ticket_id)
    return {
        "payment_id": payment.payment_id,
        "status": payment.status,
        "amount": payment.amount,
        "reason": reason,
        "reminder": "Send the customer a refund confirmation and a thank-you message.",
    }


# ======================================================================
# ticket
# ======================================================================


@tool(
    "ticket.create",
    "Create a ticket. Do this once at the start of each conversation.",
    {
        "type": "object",
        "properties": {
            "phone": {"type": "string", "description": "Customer phone if already known"}
        },
    },
)
def ticket_create(ctx: ToolContext, phone: str = "") -> dict[str, Any]:
    ticket = ctx.world.create_ticket(phone)
    ticket.owner_agent = ctx.agent_name
    return {
        "ticket_id": ticket.ticket_id,
        "status": ticket.status,
        "message": "Ticket created. Advance it with ticket.update_status at each key point.",
    }


@tool(
    "ticket.update_status",
    "Advance the ticket status. The state machine validates the transition, and key states "
    "cannot be skipped. Once a process is closed (after the thank-you message), set it to Closed.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "status": {"type": "string", "description": "Target status"},
            "note": {"type": "string", "description": "Why it changed"},
        },
        "required": ["ticket_id", "status"],
    },
)
def ticket_update_status(
    ctx: ToolContext, ticket_id: str, status: str, note: str = ""
) -> dict[str, Any]:
    world = ctx.world
    before = world.get_ticket(ticket_id).status
    ticket = world.transition_ticket(ticket_id, status, note)
    allowed = set(world.states_cfg["transitions"].get(ticket.status, []))
    allowed |= set(world.states_cfg.get("universal_targets", []))
    return {
        "ticket_id": ticket_id,
        "from": before,
        "status": ticket.status,
        "next_allowed": sorted(allowed),
    }


@tool(
    "ticket.get",
    "Get the ticket's current status, history and recorded fields. Check this when you are "
    "unsure what to do next.",
    {
        "type": "object",
        "properties": {"ticket_id": {"type": "string"}},
        "required": ["ticket_id"],
    },
)
def ticket_get(ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    world = ctx.world
    ticket = world.get_ticket(ticket_id)
    allowed = set(world.states_cfg["transitions"].get(ticket.status, []))
    allowed |= set(world.states_cfg.get("universal_targets", []))
    return {
        "ticket_id": ticket.ticket_id,
        "status": ticket.status,
        "customer_phone": ticket.customer_phone,
        "owner_agent": ticket.owner_agent,
        "fields": ticket.tags,
        "history": ticket.history,
        "next_allowed": sorted(allowed),
    }


@tool(
    "ticket.set_fields",
    "Record what you have collected on the ticket (name, address, problem, risk, property "
    "type, classification, and so on). You must do this before handing off to another agent.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "fields": {
                "type": "object",
                "description": 'Key-value pairs, e.g. {"customer_name":"Emily Carter",'
                '"address":"...","issue":"kitchen leak","risk":"ongoing drip",'
                '"category":"small_job"}',
                "additionalProperties": True,
            },
        },
        "required": ["ticket_id", "fields"],
    },
)
def ticket_set_fields(
    ctx: ToolContext, ticket_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    ticket = ctx.world.get_ticket(ticket_id)
    if not isinstance(fields, dict):
        raise ToolRejection("fields must be a JSON object")
    ticket.tags.update(fields)
    return {"ticket_id": ticket_id, "fields": ticket.tags}


# ======================================================================
# handoff / conversation
# ======================================================================


@tool(
    "handoff.transfer",
    "Hand the ticket to the agent who handles this kind of work, with a full background "
    "summary. After this you are no longer responsible for the job — do not keep talking "
    "to the customer.",
    {
        "type": "object",
        "properties": {
            "to_agent": {
                "type": "string",
                "description": "Target agent: small_job / large_job / emergency / warranty",
            },
            "reason": {"type": "string", "description": "Why it belongs to that agent"},
            "summary": {
                "type": "string",
                "description": "Handover summary: customer name, phone, address, the problem, "
                "any risk, what you have already told them, and what they want",
            },
        },
        "required": ["to_agent", "reason", "summary"],
    },
)
def handoff_transfer(
    ctx: ToolContext, to_agent: str, reason: str, summary: str
) -> dict[str, Any]:
    world = ctx.world
    allowed = ctx.scenario.get("_handoff_targets", [])
    if allowed and to_agent not in allowed:
        raise ToolRejection(
            f"{ctx.agent_name} may not hand off to '{to_agent}'. Allowed targets: {allowed}"
        )
    ctx.handoff_request = {"to_agent": to_agent, "reason": reason, "summary": summary}
    world.handoffs.append(
        {
            "from_agent": ctx.agent_name,
            "to_agent": to_agent,
            "reason": reason,
            "summary": summary,
            "at": world.now().isoformat(),
        }
    )
    return {
        "transferred_to": to_agent,
        "message": f"Handed off to {to_agent}. Your part is done; do not reply to the customer again.",
    }


@tool(
    "conversation.end",
    "Declare the conversation finished and the process closed. Before calling this you must "
    "have sent any required thank-you message and moved the ticket to an appropriate final state.",
    {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why it closed, e.g. general enquiry ended / customer not "
                "proceeding / handed off / warranty not eligible and customer declined",
            }
        },
        "required": ["reason"],
    },
)
def conversation_end(ctx: ToolContext, reason: str) -> dict[str, Any]:
    ctx.conversation_ended = True
    ctx.end_reason = reason
    return {"ended": True, "reason": reason}

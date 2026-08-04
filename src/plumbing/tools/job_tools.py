"""Tools for what happens after a technician is on site, and for formal project quotes.

Not granted to any agent yet — registered, tested and visible in the console's Tools tab,
but no agent's allow-list includes them. Wiring them up is a separate step.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from plumbing.tools.registry import ToolContext, tool
from plumbing.world import ToolRejection, _parse_dt


# ======================================================================
# technician outcome
# ======================================================================


@tool(
    "technician.get_job_outcome",
    "Check whether the technician who attended has reported back. They report one of two "
    "things: the work is done, or the customer decided not to go ahead. Either way the job "
    "is finished from your side — send the customer a thank-you message and close the "
    "ticket. If nothing has come back yet, use clock.advance rather than checking in a loop.",
    {
        "type": "object",
        "properties": {
            "appointment_id": {"type": "string", "description": "The appointment they attended"}
        },
        "required": ["appointment_id"],
    },
)
def technician_get_job_outcome(ctx: ToolContext, appointment_id: str) -> dict[str, Any]:
    world = ctx.world
    appointment = world.get_appointment(appointment_id)

    existing = world.job_outcomes.get(appointment_id)
    if existing and existing["status"] == "reported":
        return existing

    behavior = world.job_outcome_behavior
    delay = int(behavior.get("delay_minutes", 120))
    ready_at = appointment.start + timedelta(minutes=delay)
    if world.now() < ready_at:
        return {
            "appointment_id": appointment_id,
            "status": "pending",
            "expected_after": ready_at.isoformat(),
            "note": "The technician is still on the job. Advance the clock and check again.",
        }

    result = behavior.get("result", "completed")     # completed | declined
    if result not in ("completed", "declined"):
        raise ToolRejection(f"Unexpected job outcome '{result}' configured for this run.")

    technician = world.technicians.get(appointment.technician_id or "")
    reason = behavior.get("reason", "")
    spoken = ""
    if ctx.technician_sim is not None and technician is not None:
        spoken = ctx.technician_sim.report_job_outcome(
            technician=technician,
            job_summary=appointment.description,
            result=result,
            reason=reason,
        )

    if technician:
        technician.status = "completed"
        technician.active_jobs = max(0, technician.active_jobs - 1)

    outcome = {
        "appointment_id": appointment_id,
        "ticket_id": appointment.ticket_id,
        "status": "reported",
        "result": result,
        "technician_name": technician.name if technician else "",
        "technician_reply": spoken,
        "reason": reason or spoken,
        "reported_at": world.now().isoformat(),
        "next_step": "Send the customer a thank-you message and close the ticket."
        if result == "completed"
        else "The customer chose not to proceed. Send a thank-you message and close the ticket.",
    }
    world.job_outcomes[appointment_id] = outcome
    return outcome


# ======================================================================
# formal quotes (large projects)
# ======================================================================


@tool(
    "quote.create",
    "Record a formal quote a human has prepared for a large project. You do not set the "
    "price — it comes from the person who reviewed the customer's material.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "scope": {"type": "string", "description": "What the work covers"},
            "amount": {"type": "number", "description": "Total, in CAD"},
            "duration_days": {"type": "integer", "description": "Expected working days"},
            "valid_days": {"type": "integer", "description": "How long the price holds"},
            "terms": {"type": "string"},
        },
        "required": ["ticket_id", "scope", "amount"],
    },
)
def quote_create(
    ctx: ToolContext,
    ticket_id: str,
    scope: str,
    amount: float,
    duration_days: int = 0,
    valid_days: int = 30,
    terms: str = "",
) -> dict[str, Any]:
    world = ctx.world
    world.get_ticket(ticket_id)
    if amount <= 0:
        raise ToolRejection("A quote amount must be greater than zero.")
    quote = {
        "quote_id": world.next_id("QT"),
        "ticket_id": ticket_id,
        "scope": scope,
        "amount": amount,
        "currency": world.rules["company"]["currency"],
        "duration_days": duration_days,
        "valid_until": (world.now() + timedelta(days=valid_days)).date().isoformat(),
        "terms": terms,
        "status": "draft",
        "created_at": world.now().isoformat(),
    }
    world.quotes[ticket_id] = quote
    return {**quote, "reminder": "Use quote.mark_sent once you have emailed it to the customer."}


@tool(
    "quote.mark_sent",
    "Record that the formal quote has been emailed to the customer. Call it after the "
    "email actually goes out, so follow-up timing runs from the real send.",
    {
        "type": "object",
        "properties": {"ticket_id": {"type": "string"}},
        "required": ["ticket_id"],
    },
)
def quote_mark_sent(ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    world = ctx.world
    quote = world.quotes.get(ticket_id)
    if not quote:
        raise ToolRejection(f"No quote on {ticket_id}. Create it with quote.create first.")
    quote["status"] = "sent"
    quote["sent_at"] = world.now().isoformat()
    reminders = world.rules["quote_followup"]["reminders_hours"]
    return {
        **quote,
        "followup_hours": reminders,
        "reminder": f"Schedule reminders at {reminders} hours with schedule.create_followup.",
    }


@tool(
    "quote.record_decision",
    "Record the customer's answer to a formal quote.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "accepted": {"type": "boolean"},
            "reason": {
                "type": "string",
                "description": "Why, if they declined — price, timing, approach, not needed "
                "now, went elsewhere, other",
            },
        },
        "required": ["ticket_id", "accepted"],
    },
)
def quote_record_decision(
    ctx: ToolContext, ticket_id: str, accepted: bool, reason: str = ""
) -> dict[str, Any]:
    world = ctx.world
    quote = world.quotes.get(ticket_id)
    if not quote:
        raise ToolRejection(f"No quote on {ticket_id}.")
    if quote["status"] not in ("sent", "draft"):
        raise ToolRejection(f"This quote is already {quote['status']}.")
    quote["status"] = "accepted" if accepted else "declined"
    quote["decided_at"] = world.now().isoformat()
    quote["decline_reason"] = reason
    # Any pending chasing is now pointless.
    for followup in world.followups:
        if followup["ticket_id"] == ticket_id and followup["status"] == "scheduled":
            followup["status"] = "cancelled"
    return {**quote, "followups_cancelled": True}


@tool(
    "quote.get",
    "Look up the quote on a ticket and its current status.",
    {
        "type": "object",
        "properties": {"ticket_id": {"type": "string"}},
        "required": ["ticket_id"],
    },
)
def quote_get(ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    quote = ctx.world.quotes.get(ticket_id)
    if not quote:
        return {"ticket_id": ticket_id, "found": False, "message": "No quote on this ticket."}
    return {"found": True, **quote}


# ======================================================================
# scheduled follow-ups
# ======================================================================


@tool(
    "schedule.create_followup",
    "Schedule a future follow-up on a ticket — the quote reminders at 24, 48 and 72 hours, "
    "for instance. Read the exact hours from the rules rather than assuming them.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "in_hours": {"type": "number", "description": "Hours from now"},
            "purpose": {
                "type": "string",
                "enum": ["quote_reminder", "final_quote_reminder", "check_in", "other"],
            },
            "note": {"type": "string", "description": "What the follow-up should say or do"},
        },
        "required": ["ticket_id", "in_hours", "purpose"],
    },
)
def schedule_create_followup(
    ctx: ToolContext, ticket_id: str, in_hours: float, purpose: str, note: str = ""
) -> dict[str, Any]:
    world = ctx.world
    world.get_ticket(ticket_id)
    if in_hours <= 0:
        raise ToolRejection("A follow-up has to be in the future.")
    entry = {
        "followup_id": world.next_id("FU"),
        "ticket_id": ticket_id,
        "purpose": purpose,
        "note": note,
        "due_at": (world.now() + timedelta(hours=in_hours)).isoformat(),
        "status": "scheduled",
        "created_at": world.now().isoformat(),
    }
    world.followups.append(entry)
    return entry


@tool(
    "schedule.list_due",
    "List follow-ups that are now due. Anything returned here needs acting on before it is "
    "marked done.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string", "description": "Optional, to narrow to one ticket"}
        },
    },
)
def schedule_list_due(ctx: ToolContext, ticket_id: str = "") -> dict[str, Any]:
    world = ctx.world
    now = world.now()
    due = [
        f
        for f in world.followups
        if f["status"] == "scheduled"
        and (not ticket_id or f["ticket_id"] == ticket_id)
        and _parse_dt(f["due_at"], world.tz) <= now
    ]
    return {"due": due, "count": len(due), "now": now.isoformat()}


@tool(
    "schedule.mark_done",
    "Mark a follow-up as carried out, so it is not repeated.",
    {
        "type": "object",
        "properties": {
            "followup_id": {"type": "string"},
            "outcome": {"type": "string", "description": "What happened"},
        },
        "required": ["followup_id"],
    },
)
def schedule_mark_done(ctx: ToolContext, followup_id: str, outcome: str = "") -> dict[str, Any]:
    for followup in ctx.world.followups:
        if followup["followup_id"] == followup_id:
            if followup["status"] != "scheduled":
                raise ToolRejection(f"That follow-up is already {followup['status']}.")
            followup["status"] = "done"
            followup["outcome"] = outcome
            return followup
    raise ToolRejection(f"No follow-up with id '{followup_id}'.")


@tool(
    "schedule.cancel",
    "Cancel a scheduled follow-up — the customer has replied, or the job has moved on.",
    {
        "type": "object",
        "properties": {
            "followup_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["followup_id"],
    },
)
def schedule_cancel(ctx: ToolContext, followup_id: str, reason: str = "") -> dict[str, Any]:
    for followup in ctx.world.followups:
        if followup["followup_id"] == followup_id:
            followup["status"] = "cancelled"
            followup["cancel_reason"] = reason
            return followup
    raise ToolRejection(f"No follow-up with id '{followup_id}'.")

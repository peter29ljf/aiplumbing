"""Communication tools: SMS, email, phoning technicians, document uploads, escalation.

Messages land in the world's outboxes so tests can assert on their content. Phoning a
technician invokes the simulator, which decides accept / decline / no answer.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from plumbing.integrations import LiveToolUnavailable, is_live
from plumbing.tools.registry import ToolContext, tool
from plumbing.world import CallRecord, ToolRejection, normalize_phone

# ======================================================================
# sms
# ======================================================================


@tool(
    "sms.send",
    "Send a text message to a customer or technician. All confirmations, payment links, "
    "reminders, cancellations, refunds and thank-you messages go through here. The body "
    "must not contain any amount you have not looked up with a rules tool.",
    {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient phone number, or a technician id such as t_wang",
            },
            "body": {"type": "string", "description": "Message body"},
            "purpose": {
                "type": "string",
                "enum": [
                    "appointment_confirmation",
                    "reschedule_confirmation",
                    "cancellation_confirmation",
                    "emergency_confirmation",
                    "deposit_link",
                    "deposit_reminder",
                    "refund_confirmation",
                    "quote_notification",
                    "quote_reminder",
                    "upload_link",
                    "technician_dispatch",
                    "status_update",
                    "thanks_closing",
                    "other",
                ],
                "description": "What the message is for. The thank-you message that closes "
                "a process must use thanks_closing.",
            },
        },
        "required": ["to", "body", "purpose"],
    },
)
def sms_send(ctx: ToolContext, to: str, body: str, purpose: str) -> dict[str, Any]:
    world = ctx.world
    if not body.strip():
        raise ToolRejection("Message body cannot be empty")

    recipient_type = "technician" if to in world.technicians else "customer"
    resolved = (
        world.technicians[to].phone if recipient_type == "technician" else normalize_phone(to)
    )
    if recipient_type == "customer" and not resolved:
        raise ToolRejection(f"'{to}' is neither a valid phone number nor a technician id.")

    record = {
        "index": len(world.sms_outbox),
        "at": world.now().isoformat(),
        "agent": ctx.agent_name,
        "to": resolved,
        "recipient_type": recipient_type,
        "recipient_id": to if recipient_type == "technician" else resolved,
        "purpose": purpose,
        "body": body,
        "live": False,
    }

    # Real delivery only when both switches in config/tool_catalog.yaml are on.
    if is_live("sms.send"):
        from plumbing.integrations import twilio_sms  # noqa: PLC0415

        try:
            sent = twilio_sms.send_sms(resolved, body)
        except LiveToolUnavailable as exc:
            raise ToolRejection(f"Live SMS failed: {exc}") from exc
        record["live"] = True
        record["provider_message_id"] = sent["message_id"]
        world.sms_outbox.append(record)
        return {"sent": True, "message_id": sent["message_id"], "to": resolved, "live": True}

    world.sms_outbox.append(record)
    return {"sent": True, "message_id": f"SMS-{record['index'] + 1}", "to": resolved}


# ======================================================================
# email
# ======================================================================


@tool(
    "email.send",
    "Send an email. Used for formal large-project quotes and document upload links.",
    {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "purpose": {
                "type": "string",
                "enum": ["formal_quote", "upload_link", "other"],
            },
        },
        "required": ["to", "subject", "body", "purpose"],
    },
)
def email_send(
    ctx: ToolContext, to: str, subject: str, body: str, purpose: str
) -> dict[str, Any]:
    world = ctx.world
    if "@" not in to:
        raise ToolRejection(f"'{to}' is not a valid email address. Confirm it with the customer.")
    record = {
        "index": len(world.email_outbox),
        "at": world.now().isoformat(),
        "agent": ctx.agent_name,
        "to": to,
        "subject": subject,
        "body": body,
        "purpose": purpose,
        "live": False,
    }

    if is_live("email.send"):
        from plumbing.integrations import gmail_email  # noqa: PLC0415

        try:
            sent = gmail_email.send_email(to, subject, body)
        except LiveToolUnavailable as exc:
            raise ToolRejection(f"Live email failed: {exc}") from exc
        record["live"] = True
        record["provider_message_id"] = sent["message_id"]
        world.email_outbox.append(record)
        return {"sent": True, "message_id": sent["message_id"], "to": to, "live": True}

    world.email_outbox.append(record)
    return {"sent": True, "message_id": f"EM-{record['index'] + 1}", "to": to}


# ======================================================================
# phone (contacting technicians)
# ======================================================================


@tool(
    "phone.list_available_technicians",
    "List technicians who are currently on duty, cover the customer's service area, have "
    "the required skill and are under their job limit. Call this to get the candidate list "
    "before starting an emergency search.",
    {
        "type": "object",
        "properties": {
            "area": {
                "type": "string",
                "description": "Service area id; leave empty for no restriction. Use "
                "rules.lookup on service_areas to see the ids.",
            },
            "skill": {
                "type": "string",
                "description": "Required skill, e.g. leak / burst_pipe / sewage / drain / installation",
            },
        },
    },
)
def phone_list_technicians(
    ctx: ToolContext, area: str = "", skill: str = ""
) -> dict[str, Any]:
    world = ctx.world
    candidates = []
    excluded = []
    for tech in world.technicians.values():
        reasons = []
        if not tech.on_duty:
            reasons.append("off duty")
        if area and area not in tech.areas:
            reasons.append(f"does not cover area {area}")
        if skill and skill not in tech.skills:
            reasons.append(f"lacks skill {skill}")
        if tech.active_jobs >= tech.max_concurrent_jobs:
            reasons.append("at maximum job load")
        entry = {
            "id": tech.id,
            "name": tech.name,
            "skills": tech.skills,
            "areas": tech.areas,
            "status": tech.status,
        }
        (excluded if reasons else candidates).append(
            {**entry, "excluded_because": reasons} if reasons else entry
        )
    return {"available": candidates, "excluded": excluded, "count": len(candidates)}


@tool(
    "phone.call_technician",
    "Phone one technician to ask whether they can take an emergency job. Returns accepted, "
    "declined or no answer, plus an ETA. Every call is logged — mind the round and time "
    "window limits.",
    {
        "type": "object",
        "properties": {
            "technician_id": {"type": "string", "description": "Technician id, e.g. t_wang"},
            "job_summary": {
                "type": "string",
                "description": "What you tell the technician: address, problem, urgency",
            },
            "round_number": {
                "type": "integer",
                "description": "Which calling round this is, starting at 1",
            },
        },
        "required": ["technician_id", "job_summary"],
    },
)
def phone_call_technician(
    ctx: ToolContext,
    technician_id: str,
    job_summary: str,
    round_number: int = 1,
) -> dict[str, Any]:
    world = ctx.world
    tech = world.technicians.get(technician_id)
    if not tech:
        raise ToolRejection(
            f"No technician with id '{technician_id}'. Available: {sorted(world.technicians)}"
        )

    dispatch_rules = world.rules["emergency_dispatch"]
    max_rounds = dispatch_rules["max_call_rounds"]
    rounds_used = len({c.round for c in world.call_records})
    if round_number > max_rounds or rounds_used > max_rounds:
        raise ToolRejection(
            f"The maximum of {max_rounds} calling rounds has been reached. Stop calling and "
            f"text the customer that emergency service cannot be arranged right now.",
            violation="exceeded_call_rounds",
        )

    if ctx.technician_sim is None:
        outcome = {"outcome": "accepted", "reason": "", "eta_minutes": 40}
    else:
        outcome = ctx.technician_sim(
            technician=tech, job_summary=job_summary, round_number=round_number
        )

    result = outcome.get("outcome", "accepted")
    record = CallRecord(
        round=round_number,
        technician_id=technician_id,
        called_at=world.now(),
        connected=result != "no_answer",
        outcome=result,
        reason=outcome.get("reason", ""),
        eta_minutes=outcome.get("eta_minutes"),
    )
    world.call_records.append(record)

    if result == "accepted":
        tech.status = "assigned"

    return {
        "technician_id": technician_id,
        "technician_name": tech.name,
        "technician_phone": tech.phone,
        "round_number": round_number,
        "connected": record.connected,
        "outcome": result,
        "reason": record.reason,
        "eta_minutes": record.eta_minutes,
        "rounds_used": len({c.round for c in world.call_records}),
        "max_rounds": max_rounds,
        "spoken_reply": outcome.get("spoken_reply", ""),
    }


@tool(
    "phone.set_technician_status",
    "Update a technician's job status. Use en_route when they leave, on_site when they "
    "arrive, completed when finished. Refund rules depend on this: once en_route or "
    "on_site, automatic refunds are blocked.",
    {
        "type": "object",
        "properties": {
            "technician_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["idle", "assigned", "en_route", "on_site", "completed"],
            },
        },
        "required": ["technician_id", "status"],
    },
)
def phone_set_status(ctx: ToolContext, technician_id: str, status: str) -> dict[str, Any]:
    tech = ctx.world.technicians.get(technician_id)
    if not tech:
        raise ToolRejection(f"No technician with id '{technician_id}'.")
    tech.status = status
    return {"technician_id": technician_id, "status": status}


# ======================================================================
# escalate
# ======================================================================


@tool(
    "escalate.raise",
    "Create an escalation and notify a supervisor. Complaints, disputes, demands for extra "
    "refunds, payment problems, on-site incidents, and cancellation refunds after the "
    "technician has departed all go through here. Never promise compensation or a refund "
    "on your own.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "reason": {
                "type": "string",
                "description": "Category, e.g. customer complaint / refund dispute / "
                "payment failure / on-site incident",
            },
            "details": {
                "type": "string",
                "description": "Full background: what the customer said, when, and any evidence",
            },
        },
        "required": ["ticket_id", "reason", "details"],
    },
)
def escalate_raise(
    ctx: ToolContext, ticket_id: str, reason: str, details: str
) -> dict[str, Any]:
    world = ctx.world
    ticket = world.get_ticket(ticket_id)

    if ctx.supervisor_sim is None:
        decision = {"decision": "pending", "notes": "A supervisor will review shortly."}
    else:
        decision = ctx.supervisor_sim(ticket=ticket, reason=reason, details=details)

    record = {
        "escalation_id": world.next_id("ESC"),
        "ticket_id": ticket_id,
        "reason": reason,
        "details": details,
        "raised_by": ctx.agent_name,
        "at": world.now().isoformat(),
        "decision": decision.get("decision", "pending"),
        "supervisor_notes": decision.get("notes", ""),
    }
    world.escalations.append(record)
    world.transition_ticket(ticket_id, "Escalated to Supervisor", note=reason)
    return record


# ======================================================================
# review (human adjudication of a warranty claim)
# ======================================================================


@tool(
    "review.request_warranty",
    "Send a warranty claim to the technician who did the original job, for them to decide "
    "whether it is covered. The record checks (period, address, service type) only say the "
    "claim is *possible* — whether this fault is the same workmanship is a judgement only "
    "the person who did the work can make. Call this once the record checks pass. It returns "
    "immediately with a pending review: tell the customer they do not need to wait online and "
    "will be notified.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "job_id": {
                "type": "string",
                "description": "The original job id from crm.get_warranty_candidates. Leave "
                "empty if the record is unclear and you are sending it to whoever is on duty.",
            },
            "summary": {
                "type": "string",
                "description": "What the customer reports now, why they believe it relates to "
                "the original work, and anything they have sent. This is all the technician sees.",
            },
            "technician_id": {
                "type": "string",
                "description": "Optional. Leave empty to route to the technician who did the "
                "original job. If the record does not settle it, this falls back to whoever "
                "is on duty rather than failing.",
            },
        },
        "required": ["ticket_id", "summary"],
    },
)
def review_request_warranty(
    ctx: ToolContext,
    ticket_id: str,
    summary: str,
    job_id: str = "",
    technician_id: str = "",
) -> dict[str, Any]:
    world = ctx.world
    world.get_ticket(ticket_id)

    routed_to_original = False
    if not technician_id and job_id:
        for customer in world.customers.values():
            for job in customer.jobs:
                if job.job_id == job_id:
                    technician_id = job.technician_id or ""
                    routed_to_original = bool(technician_id)
                    break

    technician = world.technicians.get(technician_id) if technician_id else None

    # An unclear record is not a dead end: the on-duty technician picks it up instead.
    if technician is None or not technician.on_duty:
        on_duty = [t for t in world.technicians.values() if t.on_duty]
        if not on_duty:
            raise ToolRejection(
                "No technician is on duty to review this claim. Escalate it instead."
            )
        technician = on_duty[0]
        technician_id = technician.id
        routed_to_original = False

    review_id = world.next_id("WR")
    delay = int(world.warranty_review_behavior.get("response_delay_minutes", 45))
    world.warranty_reviews[review_id] = {
        "review_id": review_id,
        "ticket_id": ticket_id,
        "job_id": job_id,
        "technician_id": technician_id,
        "technician_name": technician.name,
        "summary": summary,
        "routed_to_original_technician": routed_to_original,
        "requested_at": world.now().isoformat(),
        "available_at": (world.now() + timedelta(minutes=delay)).isoformat(),
        "status": "pending",
        "verdict": "",
        "reason": "",
    }
    return {
        "review_id": review_id,
        "status": "pending",
        "technician": {"id": technician.id, "name": technician.name},
        "routed_to_original_technician": routed_to_original,
        "expected_within_minutes": delay,
        "reminder": "Tell the customer they do not need to wait online — we will contact them "
        "once the technician has reviewed it. Then end your turn. Use review.get_verdict "
        "later to collect the decision.",
    }


@tool(
    "review.get_verdict",
    "Collect the original technician's decision on a warranty claim. If it is still pending, "
    "use clock.advance to let time pass before checking again — do not poll in a tight loop.",
    {
        "type": "object",
        "properties": {"review_id": {"type": "string"}},
        "required": ["review_id"],
    },
)
def review_get_verdict(ctx: ToolContext, review_id: str) -> dict[str, Any]:
    world = ctx.world
    review = world.warranty_reviews.get(review_id)
    if not review:
        raise ToolRejection(f"No warranty review with id '{review_id}'.")

    if review["status"] == "decided":
        return {k: review[k] for k in ("review_id", "status", "verdict", "reason", "technician_name")}

    from plumbing.world import _parse_dt  # noqa: PLC0415

    if world.now() < _parse_dt(review["available_at"], world.tz):
        return {
            "review_id": review_id,
            "status": "pending",
            "available_at": review["available_at"],
            "note": "The technician has not replied yet. Advance the clock and check again; "
            "the customer has already been told they will be contacted.",
        }

    behavior = world.warranty_review_behavior
    verdict = behavior.get("verdict", "approve")
    reason = behavior.get("reason", "")
    technician = world.technicians.get(review["technician_id"])

    spoken = ""
    if ctx.technician_sim is not None and technician is not None:
        spoken = ctx.technician_sim.adjudicate_warranty(
            technician=technician,
            summary=review["summary"],
            verdict=verdict,
            reason=reason,
        )

    review.update(status="decided", verdict=verdict, reason=reason or spoken, decided_at=world.now().isoformat())
    return {
        "review_id": review_id,
        "status": "decided",
        "verdict": verdict,
        "reason": reason or spoken,
        "technician_name": review["technician_name"],
        "technician_reply": spoken,
        "reminder": "Approved: book the warranty visit at no charge. Not covered: explain the "
        "technician's reason and ask whether they want it as new paid work.",
    }


# ======================================================================
# materials (photos, video and drawings, collected by email)
# ======================================================================


@tool(
    "email.request_materials",
    "Email the customer asking them to reply with photos, video or drawings. This is how "
    "all customer material is collected: they reply to the email with attachments, so the "
    "thread lives in their own mailbox and ours, and the address goes onto their record. "
    "You must have their email address first — ask for it if it is not already on file. "
    "Sending this also saves the address to the CRM record.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "email": {"type": "string", "description": "Customer email address"},
            "phone": {
                "type": "string",
                "description": "Customer phone, so the address can be saved to their record",
            },
            "asking_for": {
                "type": "string",
                "description": "Exactly what they should send, in plain language — e.g. "
                "'a photo of the pipe joint under the sink and a short video of the drip'. "
                "Vague requests come back as useless pictures.",
            },
        },
        "required": ["ticket_id", "email", "asking_for"],
    },
)
def email_request_materials(
    ctx: ToolContext, ticket_id: str, email: str, asking_for: str, phone: str = ""
) -> dict[str, Any]:
    world = ctx.world
    world.get_ticket(ticket_id)
    if "@" not in email:
        raise ToolRejection(
            f"'{email}' is not a valid email address. Confirm it with the customer — "
            f"a typo here means the request never arrives and they are left waiting."
        )

    # The address is customer data: put it on the record, not just on this ticket.
    saved_to_crm = False
    key = normalize_phone(phone) if phone else world.get_ticket(ticket_id).customer_phone
    customer = world.customers.get(key) if key else None
    if customer and customer.email != email:
        customer.email = email
        saved_to_crm = True

    subject = f"Fangxin Plumbing — photos needed for your job ({ticket_id})"
    body = (
        f"Hello,\n\n"
        f"To get you an accurate answer we need a look at the problem. Please **reply to "
        f"this email** and attach:\n\n{asking_for}\n\n"
        f"Replying to this email keeps everything on one thread, so nothing gets lost and "
        f"the technician sees it alongside your job.\n\n"
        f"Fangxin Plumbing Ltd\n{world.rules['company']['phone']}"
    )
    sent = email_send(ctx, to=email, subject=subject, body=body, purpose="materials_request")

    delay = int(world.material_behavior.get("reply_delay_minutes", 20))
    request = {
        "request_id": world.next_id("MR"),
        "ticket_id": ticket_id,
        "email": email,
        "asking_for": asking_for,
        "requested_at": world.now().isoformat(),
        "available_at": (world.now() + timedelta(minutes=delay)).isoformat(),
        "message_id": sent.get("message_id", ""),
        "status": "sent",
        "saved_to_crm": saved_to_crm,
    }
    world.material_requests.append(request)
    return {
        **request,
        "reminder": "Tell the customer to reply to that email with the attachments, and that "
        "they do not need to stay online. Use email.get_materials to check for their reply.",
    }


@tool(
    "email.get_materials",
    "Check whether the customer has replied to the materials request with attachments. If "
    "nothing has arrived yet, use clock.advance rather than checking repeatedly.",
    {
        "type": "object",
        "properties": {"ticket_id": {"type": "string"}},
        "required": ["ticket_id"],
    },
)
def email_get_materials(ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    world = ctx.world
    world.get_ticket(ticket_id)
    requests = [r for r in world.material_requests if r["ticket_id"] == ticket_id]
    if not requests:
        raise ToolRejection(
            f"No materials have been requested for {ticket_id} yet. Call "
            f"email.request_materials first."
        )

    from plumbing.world import _parse_dt  # noqa: PLC0415

    latest = requests[-1]
    behavior = world.material_behavior
    reply = behavior.get("reply", "sends")   # sends | none

    if reply == "none":
        return {
            "ticket_id": ticket_id,
            "received": False,
            "status": "no_reply",
            "note": "The customer has not replied to the email. Do not stall the job waiting "
            "on it — decide what you can from what they have told you, or say plainly what "
            "you cannot judge without it.",
        }

    if world.now() < _parse_dt(latest["available_at"], world.tz):
        return {
            "ticket_id": ticket_id,
            "received": False,
            "status": "pending",
            "available_at": latest["available_at"],
            "note": "Nothing back yet. Advance the clock and check again.",
        }

    attachments = behavior.get(
        "attachments", ["photo of the reported fault", "short video of the fault"]
    )
    record = {
        "ticket_id": ticket_id,
        "from": latest["email"],
        "received_at": world.now().isoformat(),
        "attachments": attachments,
        "note": behavior.get("customer_note", ""),
    }
    if record not in world.received_materials:
        world.received_materials.append(record)
    latest["status"] = "received"
    return {
        "ticket_id": ticket_id,
        "received": True,
        "status": "received",
        "from": latest["email"],
        "attachments": attachments,
        "customer_note": record["note"],
        "count": len(attachments),
    }

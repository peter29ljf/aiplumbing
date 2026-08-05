"""Read-only tools: clock, business rules, CRM lookups.

Prices and rules are returned verbatim from business_rules.yaml — agents are never
allowed to invent a figure.
"""

from __future__ import annotations

from typing import Any

from plumbing import config
from plumbing.tools.registry import ToolContext, tool
from plumbing.world import ToolRejection, is_valid_phone, normalize_phone

_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}}


# ======================================================================
# clock
# ======================================================================


@tool(
    "clock.now",
    "Get the current date and time, plus whether today is a working day, a Sunday or a "
    "public holiday, whether it is within business hours, and whether it is past 18:00. "
    "Call this before deciding if a standard booking is possible or which emergency rate "
    "band applies.",
    _NO_ARGS,
)
def clock_now(ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.day_context()


@tool(
    "clock.advance",
    "Advance the simulated clock by a number of minutes. Only for flows that genuinely "
    "wait, such as polling technicians during an emergency search.",
    {
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "Minutes to move forward"},
            "reason": {"type": "string", "description": "Why you are waiting"},
        },
        "required": ["minutes"],
    },
)
def clock_advance(ctx: ToolContext, minutes: int, reason: str = "") -> dict[str, Any]:
    if minutes <= 0:
        raise ToolRejection("minutes must be positive")
    if minutes > 24 * 60:
        raise ToolRejection("Cannot advance more than 24 hours at once")
    ctx.world.advance(minutes)
    return {"advanced_minutes": minutes, "reason": reason, **ctx.world.day_context()}


# ======================================================================
# rules
# ======================================================================


@tool(
    "rules.get_standard_service_fee",
    "Get the call-out fee for a standard scheduled appointment. Required before telling a "
    "customer what standard service costs.",
    _NO_ARGS,
)
def rules_standard_fee(ctx: ToolContext) -> dict[str, Any]:
    pricing = ctx.world.rules["pricing"]
    fee = pricing["standard_inspection_fee"]
    no_charge = ctx.world.rules.get("no_charge", {})
    return {
        "amount": fee["amount"],
        "currency": fee["currency"],
        "qualifier": fee["qualifier"],
        "display": f"{fee['currency']} {fee['amount']} ({fee['qualifier']})",
        "offset_rules": pricing["fee_offset"],
        # Zero is a price too. Without these the agent has to promise "free" from memory.
        "reschedule_free": bool(no_charge.get("reschedule")),
        "cancellation_free": bool(no_charge.get("cancellation")),
    }


@tool(
    "rules.get_emergency_fee",
    "Get the emergency call-out fee band that applies at a given time (defaults to now), "
    "together with the deposit rules. Required before quoting any emergency price — never "
    "work out the band yourself.",
    {
        "type": "object",
        "properties": {
            "at": {
                "type": "string",
                "description": "ISO timestamp. Leave empty for the current time.",
            }
        },
    },
)
def rules_emergency_fee(ctx: ToolContext, at: str = "") -> dict[str, Any]:
    when = None
    if at:
        from plumbing.world import _parse_dt  # noqa: PLC0415

        when = _parse_dt(at, ctx.world.tz)
    tier = ctx.world.emergency_fee_tier(when)
    deposit = ctx.world.rules["pricing"]["emergency_deposit"]
    return {
        **tier,
        "display": f"{tier['currency']} {tier['amount']} ({tier['qualifier']})",
        "deposit": deposit,
    }


@tool(
    "rules.get_schedule_policy",
    "Get working days, business hours, the Sunday and public-holiday policy, and the "
    "holiday calendar.",
    _NO_ARGS,
)
def rules_schedule(ctx: ToolContext) -> dict[str, Any]:
    sched = ctx.world.rules["schedule"]
    return {
        "working_days": "Monday through Saturday",
        "working_hours": f"{sched['working_hours']['start']}-{sched['working_hours']['end']}",
        "night_starts_at": sched["night_starts_at"],
        "sunday_policy": sched["sunday_policy"],
        "holiday_policy": sched["holiday_policy"],
        "timezone": ctx.world.rules["company"]["timezone"],
        "public_holidays": sched["public_holidays"],
        "today": ctx.world.day_context(),
    }


@tool(
    "rules.get_job_sizing",
    "Get the threshold and criteria that separate small repairs from large repairs and "
    "projects, and what to do when there is not enough information. Required before triage.",
    _NO_ARGS,
)
def rules_job_sizing(ctx: ToolContext) -> dict[str, Any]:
    no_charge = ctx.world.rules.get("no_charge", {})
    return {
        **ctx.world.rules["job_sizing"],
        "quote_free": bool(no_charge.get("quote")),
    }


@tool(
    "rules.get_safety_advisory",
    "Given the customer's description of the hazard, get the safety advice to pass on. "
    "For gas, fire, shock or danger to people it returns instructions to contact local "
    "emergency services.",
    {
        "type": "object",
        "properties": {
            "risk_description": {
                "type": "string",
                "description": "The customer's own words, e.g. 'burst pipe spraying water', "
                "'sewage backing up', 'I can smell gas'",
            }
        },
        "required": ["risk_description"],
    },
)
def rules_safety(ctx: ToolContext, risk_description: str) -> dict[str, Any]:
    safety = ctx.world.rules["safety"]
    text = (risk_description or "").lower()
    matched = [
        item
        for item in safety["advisories"]
        if any(keyword in text for keyword in _keywords(item["trigger"]))
    ]
    referral_triggers = safety["emergency_services_referral"]["triggers"]
    needs_referral = any(
        any(keyword in text for keyword in _keywords(trigger))
        for trigger in referral_triggers
    )
    return {
        "advisories": matched or safety["advisories"],
        "requires_emergency_services_referral": needs_referral,
        "emergency_services_referral": safety["emergency_services_referral"],
        "note": safety["emergency_services_referral"]["note"],
    }


@tool(
    "rules.get_warranty_policy",
    "Get the warranty policy: how long it lasts, which service types are excluded (such as "
    "drain cleaning), and the eligibility checklist.",
    _NO_ARGS,
)
def rules_warranty(ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.rules["warranty"]


@tool(
    "rules.get_escalation_policy",
    "Get the situations that must be escalated to a supervisor, and what the agent is "
    "forbidden from doing in them.",
    _NO_ARGS,
)
def rules_escalation(ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.rules["escalation"]


@tool(
    "rules.lookup",
    "Read any field from the business rules table by dotted path, e.g. "
    "'pricing.emergency_deposit.amount'. Use only when none of the specific rules tools fit.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Dot-separated config path"}
        },
        "required": ["path"],
    },
)
def rules_lookup(ctx: ToolContext, path: str) -> dict[str, Any]:
    value = config.dig(ctx.world.rules, path)
    if value is None:
        raise ToolRejection(
            f"No path '{path}' in the business rules. Top-level keys: {sorted(ctx.world.rules)}"
        )
    return {"path": path, "value": value}


# ======================================================================
# crm
# ======================================================================


@tool(
    "crm.lookup_by_phone",
    "Look up a customer by phone number: name, address, email, job history and warranty "
    "candidates. Call this as soon as you have the number, to tell new and returning "
    "customers apart.",
    {
        "type": "object",
        "properties": {
            "phone": {"type": "string", "description": "Customer phone number, any common format"}
        },
        "required": ["phone"],
    },
)
def crm_lookup(ctx: ToolContext, phone: str) -> dict[str, Any]:
    if not is_valid_phone(phone):
        raise ToolRejection(
            f"'{phone}' is not a valid North American phone number (10 digits, or 11 "
            f"starting with 1). Confirm it with the customer and try again."
        )
    world = ctx.world
    key = normalize_phone(phone)
    customer = world.find_customer(key)

    ticket = world.active_ticket()
    if ticket and not ticket.customer_phone:
        ticket.customer_phone = key

    if not customer:
        return {
            "found": False,
            "phone": key,
            "customer_type": "new",
            "message": "No history for this number — a new customer. Collect their name, "
            "service address and the problem, then call crm.create_customer.",
        }

    return {
        "found": True,
        "phone": key,
        "customer_type": "existing",
        "name": customer.name,
        "email": customer.email,
        "address": customer.address,
        "area": customer.area,
        "property_type": customer.property_type,
        "job_history": [
            {
                "job_id": j.job_id,
                "service_type": j.service_type,
                "service_name": j.service_name,
                "address": j.address,
                "completed_at": j.completed_at,
                "status": j.status,
                "amount": j.amount,
                "technician_id": j.technician_id,
            }
            for j in customer.jobs
        ],
        "open_appointments": [
            {
                "id": a.appointment_id,
                "kind": a.kind,
                "start": a.start.isoformat(),
                "status": a.status,
            }
            for a in world.appointments
            if a.customer_phone == key and a.status != "cancelled"
        ],
        # Summaries only. Call crm.get_conversation_history for the full transcripts.
        "previous_conversations": [
            {"at": c.get("at"), "channel": c.get("channel", "chat"), "summary": c.get("summary", "")}
            for c in sorted(
                (c for c in world.chat_history if c.get("phone") == key),
                key=lambda c: str(c.get("at", "")),
                reverse=True,
            )
        ],
    }


@tool(
    "crm.create_customer",
    "Create a record for a new customer. Phone, name and service address are required; "
    "email and property type are optional.",
    {
        "type": "object",
        "properties": {
            "phone": {"type": "string"},
            "name": {"type": "string"},
            "address": {"type": "string", "description": "Full service address"},
            "email": {"type": "string"},
            "property_type": {
                "type": "string",
                "enum": ["house", "townhouse", "apartment", "retail", "commercial"],
                "description": "apartment covers condo and strata units",
            },
        },
        "required": ["phone", "name", "address"],
    },
)
def crm_create_customer(
    ctx: ToolContext,
    phone: str,
    name: str,
    address: str,
    email: str = "",
    property_type: str = "",
) -> dict[str, Any]:
    from plumbing.world import Customer  # noqa: PLC0415

    if not is_valid_phone(phone):
        raise ToolRejection(f"'{phone}' is not a valid phone number; cannot create a record.")
    world = ctx.world
    key = normalize_phone(phone)
    if world.find_customer(key) is not None:
        return {
            "created": False,
            "phone": key,
            "message": "This customer already has a record; no need to create another.",
        }
    world.save_customer(Customer(
        phone=key,
        name=name,
        address=address,
        email=email,
        property_type=property_type,
        area=_infer_area(world, address),
        is_new=True,
    ))
    return {"created": True, "phone": key, "name": name, "address": address}


@tool(
    "crm.update_customer",
    "Correct or add fields on an existing customer record (name, address, email, property "
    "type). Call this when a customer corrects something you got wrong — do not simply "
    "promise to fix it.",
    {
        "type": "object",
        "properties": {
            "phone": {"type": "string"},
            "name": {"type": "string"},
            "address": {"type": "string"},
            "email": {"type": "string"},
            "property_type": {
                "type": "string",
                "enum": ["house", "townhouse", "apartment", "retail", "commercial"],
                "description": "apartment covers condo and strata units",
            },
        },
        "required": ["phone"],
    },
)
def crm_update_customer(ctx: ToolContext, phone: str, **updates: Any) -> dict[str, Any]:
    if not is_valid_phone(phone):
        raise ToolRejection(f"'{phone}' is not a valid phone number.")
    customer = ctx.world.find_customer(phone)
    if not customer:
        raise ToolRejection(
            f"No customer record found for {phone}. For a new customer, call "
            f"crm.create_customer first."
        )
    changed: dict[str, Any] = {}
    for field_name in ("name", "address", "email", "property_type"):
        value = updates.get(field_name)
        if value:
            setattr(customer, field_name, value)
            changed[field_name] = value
    if not changed:
        raise ToolRejection("No fields supplied to update.")
    ctx.world.save_customer(customer)
    return {"phone": customer.phone, "updated": changed}


@tool(
    "crm.get_warranty_candidates",
    "Find this customer's past jobs that might be under warranty, with an eligibility "
    "verdict and reasons for each. Required whenever a customer raises a warranty claim — "
    "never judge eligibility from the conversation alone.",
    {
        "type": "object",
        "properties": {
            "phone": {"type": "string"},
            "current_address": {
                "type": "string",
                "description": "The address the customer is reporting from, so it can be "
                "compared with the original service address",
            },
        },
        "required": ["phone"],
    },
)
def crm_warranty_candidates(
    ctx: ToolContext, phone: str, current_address: str = ""
) -> dict[str, Any]:
    from datetime import datetime, timedelta  # noqa: PLC0415

    world = ctx.world
    if not is_valid_phone(phone):
        raise ToolRejection(f"'{phone}' is not a valid phone number.")
    customer = world.customers.get(normalize_phone(phone))
    policy = world.rules["warranty"]
    excluded_types = {e["id"] for e in policy["excluded_services"]}

    if not customer or not customer.jobs:
        return {
            "eligible_jobs": [],
            "ineligible_jobs": [],
            "message": "No service history found for this number, so it cannot be handled "
            "under warranty.",
            "policy": policy,
        }

    eligible: list[dict[str, Any]] = []
    ineligible: list[dict[str, Any]] = []
    now = world.now()
    for job in customer.jobs:
        reasons: list[str] = []
        if job.status != "completed" or not job.completed_at:
            reasons.append("The original job was never completed")
            completed = None
        else:
            completed = datetime.fromisoformat(job.completed_at)
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=world.tz)
            expires = completed + timedelta(days=30 * policy["period_months"])
            if now > expires:
                reasons.append(
                    f"Past the {policy['period_months']}-month warranty period "
                    f"(original work completed {completed.date().isoformat()})"
                )
        if job.warranty_excluded or job.service_type in excluded_types:
            reasons.append("This service type (drain cleaning) carries no 1-year warranty")
        if current_address and job.address and not _same_address(
            current_address, job.address
        ):
            reasons.append(
                f"Current address differs from the original service address "
                f"(original: {job.address})"
            )

        record = {
            "job_id": job.job_id,
            "service_type": job.service_type,
            "service_name": job.service_name,
            "address": job.address,
            "completed_at": job.completed_at,
            "technician_id": job.technician_id,
            "warranty_expires_at": (
                (completed + timedelta(days=30 * policy["period_months"])).date().isoformat()
                if completed
                else None
            ),
        }
        if reasons:
            ineligible.append({**record, "reasons": reasons})
        else:
            eligible.append(
                {
                    **record,
                    "note": "Within the warranty period and the service type is covered. "
                    "Still confirm the current problem relates to this original work.",
                }
            )

    return {
        "eligible_jobs": eligible,
        "ineligible_jobs": ineligible,
        "policy": policy,
        "message": (
            "There is at least one job that may be covered."
            if eligible
            else "No job qualifies for warranty. Explain the reasons to the customer and ask "
            "whether they want it handled as new work."
        ),
    }


# ======================================================================
# Helpers
# ======================================================================


def _keywords(trigger: str) -> list[str]:
    """Expand a rules-file trigger phrase into matchable keywords."""
    extra = {
        "Major leak or burst pipe": [
            "leak", "leaking", "burst", "flood", "spraying", "water everywhere",
        ],
        "Sewage backup": ["sewage", "sewer", "backing up", "backup", "waste"],
        "gas smell": ["gas", "smell of gas", "smells like gas"],
        "fire risk": ["fire", "smoke", "burning"],
        "electrical shock risk": ["shock", "electric", "electrical", "sparking"],
        "risk of injury": ["injured", "injury", "hurt", "dangerous"],
    }
    keywords = [trigger.lower()]
    keywords.extend(k.lower() for k in extra.get(trigger, []))
    return keywords


def _same_address(left: str, right: str) -> bool:
    def norm(value: str) -> str:
        return "".join(ch for ch in value.lower() if ch.isalnum())

    a, b = norm(left), norm(right)
    return a in b or b in a


def _infer_area(world: Any, address: str) -> str:
    upper = (address or "").upper().replace(" ", "")
    for area in world.rules.get("service_areas", []):
        for prefix in area["postal_prefixes"]:
            if prefix in upper or area["name"].upper().replace(" ", "") in upper:
                return area["id"]
    return ""


@tool(
    "rules.get_company_info",
    "Get the company's own details: legal name, phone, service region, credentials "
    "(licensing, insurance, WCB), the services offered, and how a job runs. Use this when "
    "a customer asks who you are, what you cover, whether you are licensed or insured, or "
    "whether you serve their area. Do not describe the company from memory.",
    {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "enum": ["all", "credentials", "services", "contact", "process", "areas"],
                "description": "Narrow the answer. Defaults to all.",
            }
        },
    },
)
def rules_company_info(ctx: ToolContext, topic: str = "all") -> dict[str, Any]:
    company = ctx.world.rules["company"]
    profile = company.get("profile", {})
    areas = [
        {"id": a["id"], "name": a["name"]} for a in ctx.world.rules.get("service_areas", [])
    ]
    blocks = {
        "contact": {
            "name": company["name"],
            "phone": company["phone"],
            "website": company.get("website", ""),
            "service_region": company.get("service_region", ""),
            "note": "The phone number accepts text messages as well as calls.",
        },
        "credentials": {"credentials": profile.get("credentials", [])},
        "services": {"services": profile.get("services", [])},
        "process": {"how_a_job_runs": profile.get("how_a_job_runs", [])},
        "areas": {"service_region": company.get("service_region", ""), "areas": areas},
    }
    if topic and topic != "all" and topic in blocks:
        return blocks[topic]
    return {
        "name": company["name"],
        "tagline": profile.get("tagline", ""),
        "established": profile.get("established", ""),
        **blocks["contact"],
        **blocks["credentials"],
        **blocks["services"],
        **blocks["process"],
        "areas": areas,
    }


@tool(
    "crm.get_conversation_history",
    "Retrieve the full transcripts of this customer's previous conversations with us. "
    "crm.lookup_by_phone already returns one-line summaries; call this when you need the "
    "actual wording — a customer says 'your guy told me it was covered' or 'I was quoted X' "
    "and you need to check the record rather than take their word for it or contradict them "
    "without evidence.",
    {
        "type": "object",
        "properties": {
            "phone": {"type": "string", "description": "Customer phone number"},
            "limit": {
                "type": "integer",
                "description": "Most recent N conversations. Defaults to all.",
            },
        },
        "required": ["phone"],
    },
)
def crm_get_conversation_history(ctx: ToolContext, phone: str, limit: int = 0) -> dict[str, Any]:
    if not is_valid_phone(phone):
        raise ToolRejection(f"'{phone}' is not a valid phone number.")
    key = normalize_phone(phone)
    entries = [e for e in ctx.world.chat_history if e.get("phone") == key]
    entries.sort(key=lambda e: str(e.get("at", "")), reverse=True)
    if limit and limit > 0:
        entries = entries[:limit]
    return {
        "phone": key,
        "count": len(entries),
        "conversations": [
            {
                "at": e.get("at"),
                "channel": e.get("channel", "chat"),
                "summary": e.get("summary", ""),
                "transcript": e.get("transcript", ""),
            }
            for e in entries
        ],
        "message": "No previous conversations on record for this number."
        if not entries
        else f"{len(entries)} previous conversation(s) found.",
    }


@tool(
    "rules.check_service_eligibility",
    "Check whether we are able to take a job at all, given the property type and how big "
    "the job is. Some property types are outside our insurance cover. Call this once you "
    "know the property type and the job size, before offering anything.",
    {
        "type": "object",
        "properties": {
            "property_type": {
                "type": "string",
                "enum": ["house", "townhouse", "apartment", "retail", "commercial", "unknown"],
                "description": "apartment covers condo and strata units",
            },
            "job_size": {
                "type": "string",
                "enum": ["small_job", "large_job", "unknown"],
                "description": "Use rules.get_job_sizing to decide this first",
            },
        },
        "required": ["property_type", "job_size"],
    },
)
def rules_check_service_eligibility(
    ctx: ToolContext, property_type: str, job_size: str
) -> dict[str, Any]:
    policy = ctx.world.rules.get("service_policy", {})
    for excluded in policy.get("excluded_property_types", []):
        if excluded["id"] != property_type:
            continue
        if job_size == "large_job":
            return {
                "can_serve": True,
                "property_type": property_type,
                "job_size": job_size,
                "note": excluded["exception"].strip(),
                "requires_human_review": True,
            }
        return {
            "can_serve": False,
            "property_type": property_type,
            "job_size": job_size,
            "reason": excluded["reason"].strip(),
            "exception": excluded["exception"].strip(),
            "how_to_say_it": policy.get("decline_guidance", "").strip(),
        }
    return {"can_serve": True, "property_type": property_type, "job_size": job_size}


@tool(
    "rules.get_technician_handover_policy",
    "How long to wait before checking back with a technician who has taken a job, and what "
    "the agent is and is not responsible for once they have it. Pass your own flow to get "
    "the interval that applies to it — a quote being priced from emailed material takes "
    "longer than a booked repair.",
    {
        "type": "object",
        "properties": {
            "flow": {
                "type": "string",
                "enum": ["small_job", "emergency", "warranty", "large_job"],
                "description": "Which flow you are running. Defaults to the general interval.",
            }
        },
    },
)
def rules_technician_handover(ctx: ToolContext, flow: str = "") -> dict[str, Any]:
    policy = dict(ctx.world.rules.get("technician_handover", {}))
    by_flow = policy.pop("by_flow", {}) or {}
    hours = by_flow.get(flow, policy.get("followup_after_hours", 24))
    return {
        **policy,
        "flow": flow or "default",
        "followup_after_hours": hours,
        "all_flows": by_flow,
    }

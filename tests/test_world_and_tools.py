"""Unit tests for the tool layer and world state. No LLM calls, no tokens.

A bug down here turns every agent-level test into noise, so this layer has to prove
itself first.
"""

from __future__ import annotations

import json

import pytest

from plumbing.tools import dispatch, resolve, schemas
from plumbing.tools.registry import ToolContext
from plumbing.world import ToolRejection, World, is_valid_phone, normalize_phone

WORKDAY = "2026-08-05T10:00:00-07:00"      # Wednesday, business hours (Vancouver, PDT)
WORKDAY_NIGHT = "2026-08-05T19:30:00-07:00"
WORKDAY_EARLY = "2026-08-05T07:30:00-07:00"  # before 08:00 opening
SUNDAY = "2026-08-09T10:00:00-07:00"
HOLIDAY = "2026-08-03T10:00:00-07:00"      # BC Day


def make_world(now: str = WORKDAY, overrides: dict | None = None) -> World:
    return World(now=now, overrides=overrides)


def call(_world: World, _tool_name: str, /, **kwargs):
    """Call a tool by name through the full dispatch path (logging and hard gates included).

    The first two parameters are positional-only so they cannot collide with a tool's own
    argument names.
    """
    tools = resolve(["*.*"])
    ctx = ToolContext(world=_world, agent_name="test")
    wire = _tool_name.replace(".", "_", 1)
    return dispatch(ctx, tools, wire, json.dumps(kwargs)), ctx


# ======================================================================
# Phone numbers
# ======================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("6045550101", "+16045550101"),
        ("+1 604 555 0101", "+16045550101"),
        ("(604) 555-0101", "+16045550101"),
        ("1-604-555-0101", "+16045550101"),
        ("778 555 0105", "+17785550105"),
    ],
)
def test_phone_normalization(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("bad", ["123", "abc", "", "12345678901234"])
def test_invalid_phone_rejected(bad):
    assert not is_valid_phone(bad)


# ======================================================================
# Time context and rate bands
# ======================================================================


def test_workday_business_hours_context():
    ctx = make_world(WORKDAY).day_context()
    assert ctx["is_working_day"] is True
    assert ctx["within_business_hours"] is True
    assert ctx["standard_booking_available"] is True
    assert ctx["is_night"] is False


def test_sunday_blocks_standard_booking():
    ctx = make_world(SUNDAY).day_context()
    assert ctx["is_sunday"] is True
    assert ctx["standard_booking_available"] is False


def test_public_holiday_blocks_standard_booking():
    ctx = make_world(HOLIDAY).day_context()
    assert ctx["is_public_holiday"] is True
    assert ctx["holiday_name"] == "BC Day"
    assert ctx["standard_booking_available"] is False


@pytest.mark.parametrize(
    "now,tier_id,amount",
    [
        (WORKDAY, "workday_business_hours", 200),
        (WORKDAY_EARLY, "workday_offhours_before_18", 300),
        (WORKDAY_NIGHT, "night_after_18", 400),
        (SUNDAY, "sunday_or_holiday", 400),
        (HOLIDAY, "sunday_or_holiday", 400),
    ],
)
def test_emergency_fee_tiers(now, tier_id, amount):
    tier = make_world(now).emergency_fee_tier()
    assert tier["tier_id"] == tier_id
    assert tier["amount"] == amount


def test_emergency_fee_tool_quotes_the_band_and_no_deposit():
    """The business stopped taking a deposit. An agent still offering to collect one is
    an agent asking a customer for money nobody is owed."""
    result, _ = call(make_world(WORKDAY_NIGHT), "rules.get_emergency_fee")
    assert result["ok"]
    assert result["amount"] == 400
    assert result["deposit"] is None


# ======================================================================
# CRM
# ======================================================================


def test_lookup_existing_customer():
    result, _ = call(make_world(), "crm.lookup_by_phone", phone="604-555-0101")
    assert result["ok"] and result["found"]
    assert result["customer_type"] == "existing"
    assert result["name"] == "Linda Zhang"
    assert len(result["job_history"]) == 1


def test_lookup_unknown_number_is_new_customer():
    result, _ = call(make_world(), "crm.lookup_by_phone", phone="778-555-9999")
    assert result["ok"] and not result["found"]
    assert result["customer_type"] == "new"


def test_lookup_rejects_malformed_number():
    result, _ = call(make_world(), "crm.lookup_by_phone", phone="123")
    assert not result["ok"]
    assert "not a valid" in result["error"]


def test_create_customer_marks_new():
    world = make_world()
    result, _ = call(
        world,
        "crm.create_customer",
        phone="778-555-9999",
        name="Test Customer",
        address="8888 Cambie Rd, Richmond, BC V6X 1K3",
    )
    assert result["created"] is True
    assert world.customers["+17785559999"].is_new is True
    assert world.snapshot()["customers_created"] == ["+17785559999"]


# ======================================================================
# Warranty eligibility
# ======================================================================


def test_warranty_eligible_within_one_year():
    result, _ = call(
        make_world(),
        "crm.get_warranty_candidates",
        phone="+16045550101",
        current_address="5900 No. 3 Rd, Richmond, BC V6X 3P7",
    )
    assert len(result["eligible_jobs"]) == 1
    assert result["eligible_jobs"][0]["job_id"] == "J-2025-0412"


def test_warranty_excluded_for_drain_cleaning():
    result, _ = call(make_world(), "crm.get_warranty_candidates", phone="+16045550102")
    assert result["eligible_jobs"] == []
    reasons = result["ineligible_jobs"][0]["reasons"]
    assert any("drain cleaning" in r for r in reasons)


def test_warranty_expired_after_one_year():
    result, _ = call(make_world(), "crm.get_warranty_candidates", phone="+16045550103")
    assert result["eligible_jobs"] == []
    reasons = result["ineligible_jobs"][0]["reasons"]
    assert any("warranty period" in r for r in reasons)


def test_warranty_address_mismatch_flagged():
    result, _ = call(
        make_world(),
        "crm.get_warranty_candidates",
        phone="+16045550101",
        current_address="999 Completely Different Rd, Langley",
    )
    assert result["eligible_jobs"] == []
    assert any("address" in r for r in result["ineligible_jobs"][0]["reasons"])


# ======================================================================
# Ticket state machine
# ======================================================================


def test_legal_transition_chain():
    world = make_world()
    ticket = world.create_ticket("+16045550101")
    for status in ["Phone Verified", "Customer Identified", "Needs Assessment"]:
        world.transition_ticket(ticket.ticket_id, status)
    assert ticket.status == "Needs Assessment"
    assert len(ticket.history) == 4


def test_illegal_transition_rejected():
    world = make_world()
    ticket = world.create_ticket()
    with pytest.raises(ToolRejection) as exc:
        world.transition_ticket(ticket.ticket_id, "Emergency Job Dispatched")
    assert "Cannot go straight from" in str(exc.value)


def test_illegal_transition_records_violation_through_dispatch():
    world = make_world()
    ticket = world.create_ticket()
    result, _ = call(
        world, "ticket.update_status", ticket_id=ticket.ticket_id, status="Deposit Paid"
    )
    assert not result["ok"]
    assert world.violations[0]["kind"] == "illegal_ticket_transition"


def test_closed_is_reachable_from_anywhere():
    world = make_world()
    ticket = world.create_ticket()
    world.transition_ticket(ticket.ticket_id, "Closed")
    assert ticket.status == "Closed"


# ======================================================================
# Calendar hard gates
# ======================================================================


def test_find_slots_skips_sunday():
    world = make_world(now="2026-08-08T17:00:00-07:00")  # Saturday evening
    slots = world.find_slots(limit=3)
    assert slots
    for slot in slots:
        from datetime import datetime

        assert datetime.fromisoformat(slot["start"]).weekday() != 6


def test_cannot_book_standard_on_sunday():
    world = make_world(SUNDAY)
    ticket = world.create_ticket("+16045550101")
    result, _ = call(
        world,
        "calendar.create_appointment",
        ticket_id=ticket.ticket_id,
        kind="standard",
        phone="+16045550101",
        start="2026-08-09T14:00:00-07:00",
        address="5900 No. 3 Rd",
        description="leak",
        technician_id="t_wang",
    )
    assert not result["ok"]
    assert world.violations[-1]["kind"] == "booking_on_closed_day"


def test_cannot_book_standard_on_holiday():
    world = make_world(HOLIDAY)
    ticket = world.create_ticket("+16045550101")
    result, _ = call(
        world,
        "calendar.create_appointment",
        ticket_id=ticket.ticket_id,
        kind="standard",
        phone="+16045550101",
        start="2026-08-03T14:00:00-07:00",
        address="5900 No. 3 Rd",
        description="leak",
        technician_id="t_wang",
    )
    assert not result["ok"]
    assert "BC Day" in result["error"]


def test_emergency_booking_allowed_on_sunday_with_deposit():
    world = make_world(SUNDAY)
    ticket = world.create_ticket("+16045550101")
    call(world, "payment.send_deposit_link", ticket_id=ticket.ticket_id, phone="+16045550101")
    call(world, "payment.check_status", ticket_id=ticket.ticket_id)
    result, _ = call(
        world,
        "calendar.create_appointment",
        ticket_id=ticket.ticket_id,
        kind="emergency",
        phone="+16045550101",
        start="2026-08-09T14:00:00-07:00",
        address="5900 No. 3 Rd",
        description="burst pipe",
        technician_id="t_li",
    )
    assert result["ok"]


# ======================================================================
# Deposit hard gate
# ======================================================================


def test_dispatch_blocked_without_deposit():
    """Only where a deposit is actually charged. With none configured there is nothing to
    clear, and holding a burst pipe behind a payment nobody asked for would be worse than
    the gate it replaced."""
    world = _charging_a_deposit(make_world())
    ticket = world.create_ticket("+16045550101")
    result, _ = call(
        world,
        "calendar.create_appointment",
        ticket_id=ticket.ticket_id,
        kind="emergency",
        phone="+16045550101",
        start=WORKDAY,
        address="5900 No. 3 Rd",
        description="burst pipe",
        technician_id="t_li",
    )
    assert not result["ok"]
    assert "deposit has not been paid" in result["error"]
    assert world.violations[-1]["kind"] == "dispatch_before_deposit"


def test_payment_failure_scenario_keeps_dispatch_blocked():
    world = _charging_a_deposit(make_world(overrides={"payment": {"default_outcome": "fail"}}))
    ticket = world.create_ticket("+16045550101")
    call(world, "payment.send_deposit_link", ticket_id=ticket.ticket_id, phone="+16045550101")
    status, _ = call(world, "payment.check_status", ticket_id=ticket.ticket_id)
    assert status["status"] == "failed"
    assert not world.deposit_paid(ticket.ticket_id)


def test_payment_pending_scenario_never_pays():
    world = _charging_a_deposit(make_world(overrides={"payment": {"default_outcome": "pending"}}))
    ticket = world.create_ticket("+16045550101")
    call(world, "payment.send_deposit_link", ticket_id=ticket.ticket_id, phone="+16045550101")
    status, _ = call(world, "payment.check_status", ticket_id=ticket.ticket_id)
    assert status["paid"] is False


# ======================================================================
# Refund hard gate
# ======================================================================


def _charging_a_deposit(world: World) -> World:
    """Put a deposit back into this one world's rules.

    The business stopped taking one, so `config/business_rules.yaml` says
    `emergency_deposit: null` and nothing a customer meets asks for money up front. The
    refund gate underneath is still worth its coverage — when the cut-off is the
    confirmation message rather than an internal flag is a genuinely subtle rule, and it
    was written after a real argument about a van already on the road.

    `world.rules` is a fresh copy per world, so this changes nothing outside this test.
    """
    world.rules["pricing"]["emergency_deposit"] = {
        "amount": 100, "currency": "CAD", "refundable": True,
        "offsets_inspection_fee": True, "note": "",
    }
    world.rules["emergency_dispatch"]["deposit_required_before_dispatch"] = True
    return world


def _emergency_with_paid_deposit(world: World) -> str:
    _charging_a_deposit(world)
    ticket = world.create_ticket("+16045550101")
    call(world, "payment.send_deposit_link", ticket_id=ticket.ticket_id, phone="+16045550101")
    call(world, "payment.check_status", ticket_id=ticket.ticket_id)
    call(
        world,
        "calendar.create_appointment",
        ticket_id=ticket.ticket_id,
        kind="emergency",
        phone="+16045550101",
        start=WORKDAY,
        address="5900 No. 3 Rd",
        description="burst pipe",
        technician_id="t_li",
    )
    return ticket.ticket_id


def test_refund_allowed_before_departure():
    world = make_world()
    ticket_id = _emergency_with_paid_deposit(world)
    result, _ = call(world, "payment.refund_deposit", ticket_id=ticket_id, reason="customer cancelled")
    assert result["ok"] and result["status"] == "refunded"


def test_refund_blocked_once_the_customer_has_been_told_someone_is_coming():
    """The cut-off is the confirmation message, not an internal status.

    A technician who accepts an emergency call sets off immediately, so waiting for an
    "en route" flag would leave a window where the van is moving but the system still
    thinks a refund is free — exactly the window a customer cancels in.
    """
    world = make_world()
    ticket_id = _emergency_with_paid_deposit(world)

    # Still refundable while nobody has been told anything
    assert world.dispatch_confirmed(ticket_id) is False

    call(world, "sms.send", to="+16045550101",
         body="David Li is on his way, ETA 40 minutes.",
         purpose="emergency_confirmation")

    result, _ = call(world, "payment.refund_deposit", ticket_id=ticket_id)
    assert not result["ok"]
    assert "already been sent the emergency confirmation" in result["error"]
    assert world.violations[-1]["kind"] == "auto_refund_after_dispatch_confirmed"


def test_refund_still_blocked_by_a_moving_technician_without_a_message():
    """However it came about, a technician already travelling closes the window too."""
    world = make_world()
    ticket_id = _emergency_with_paid_deposit(world)
    call(world, "phone.set_technician_status", technician_id="t_li", status="on_site")
    result, _ = call(world, "payment.refund_deposit", ticket_id=ticket_id)
    assert not result["ok"]


def test_a_confirmation_to_a_different_customer_does_not_block_this_refund():
    """One customer's dispatch must not freeze another customer's deposit."""
    world = make_world()
    ticket_id = _emergency_with_paid_deposit(world)
    call(world, "sms.send", to="+16045550102",
         body="Someone is on the way.", purpose="emergency_confirmation")
    result, _ = call(world, "payment.refund_deposit", ticket_id=ticket_id)
    assert result["ok"] and result["status"] == "refunded"


def test_double_refund_rejected():
    world = make_world()
    ticket_id = _emergency_with_paid_deposit(world)
    call(world, "payment.refund_deposit", ticket_id=ticket_id)
    result, _ = call(world, "payment.refund_deposit", ticket_id=ticket_id)
    assert not result["ok"]
    assert "already been refunded" in result["error"]


# ======================================================================
# Technician calling round ceiling
# ======================================================================


def test_call_rounds_capped_at_six():
    world = make_world()
    tools = resolve(["*.*"])
    ctx = ToolContext(
        world=world,
        agent_name="test",
        technician_sim=lambda **_: {"outcome": "declined", "reason": "busy"},
    )
    for round_number in range(1, 7):
        out = dispatch(
            ctx,
            tools,
            "phone_call_technician",
            json.dumps(
                {"technician_id": "t_li", "job_summary": "burst pipe", "round_number": round_number}
            ),
        )
        assert out["ok"], out

    out = dispatch(
        ctx,
        tools,
        "phone_call_technician",
        json.dumps({"technician_id": "t_li", "job_summary": "burst pipe", "round_number": 7}),
    )
    assert not out["ok"]
    assert world.violations[-1]["kind"] == "exceeded_call_rounds"


def test_available_technicians_excludes_off_duty():
    result, _ = call(make_world(), "phone.list_available_technicians", area="richmond")
    ids = {t["id"] for t in result["available"]}
    assert "t_zhao" not in ids  # off duty by default
    assert "t_li" in ids


# ======================================================================
# Registry and permission isolation
# ======================================================================


def test_whitelist_hides_unlisted_tools():
    world = make_world()
    tools = resolve(["clock.*", "rules.*"])
    ctx = ToolContext(world=world, agent_name="intake")
    out = dispatch(ctx, tools, "payment_refund_deposit", "{}")
    assert not out["ok"]
    assert "does not exist or you are not permitted" in out["error"]
    assert world.violations[-1]["kind"] == "unknown_tool"


def test_wire_names_are_openai_compatible():
    import re

    for schema in schemas(resolve(["*.*"])):
        name = schema["function"]["name"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name), name


def test_bad_json_arguments_returned_as_error_not_crash():
    world = make_world()
    tools = resolve(["*.*"])
    ctx = ToolContext(world=world, agent_name="test")
    out = dispatch(ctx, tools, "clock_advance", "{not json")
    assert not out["ok"]
    assert "Could not parse arguments" in out["error"]


def test_handoff_respects_allowed_targets():
    world = make_world()
    tools = resolve(["*.*"])
    ctx = ToolContext(
        world=world, agent_name="intake", scenario={"_handoff_targets": ["small_job"]}
    )
    ok = dispatch(
        ctx,
        tools,
        "handoff_transfer",
        json.dumps({"to_agent": "small_job", "reason": "small job", "summary": "leak"}),
    )
    assert ok["ok"]
    assert ctx.handoff_request["to_agent"] == "small_job"

    bad = dispatch(
        ctx,
        tools,
        "handoff_transfer",
        json.dumps({"to_agent": "emergency", "reason": "x", "summary": "y"}),
    )
    assert not bad["ok"]


# ======================================================================
# Clock advance and escalation
# ======================================================================


def test_clock_advance_moves_into_night_tier():
    world = make_world(now="2026-08-05T17:30:00-07:00")
    assert world.emergency_fee_tier()["amount"] == 200
    call(world, "clock.advance", minutes=60, reason="waiting on technician replies")
    assert world.emergency_fee_tier()["amount"] == 400


def test_escalation_sets_ticket_state_and_records():
    world = make_world()
    ticket = world.create_ticket("+16045550101")
    result, _ = call(
        world,
        "escalate.raise",
        ticket_id=ticket.ticket_id,
        reason="customer complaint",
        details="customer disputes the call-out fee",
    )
    assert result["ok"]
    assert ticket.status == "Escalated to Supervisor"
    assert world.escalations[0]["reason"] == "customer complaint"


def test_sms_records_purpose_and_recipient_type():
    world = make_world()
    call(world, "sms.send", to="+16045550101", body="Thanks for calling us today.", purpose="thanks_closing")
    call(world, "sms.send", to="t_wang", body="New dispatch", purpose="technician_dispatch")
    assert world.sms_outbox[0]["recipient_type"] == "customer"
    assert world.sms_outbox[1]["recipient_type"] == "technician"
    assert world.sms_outbox[1]["to"] == "+1-604-555-0201"


# ======================================================================
# Thread safety (scenarios run in parallel)
# ======================================================================


def test_registry_is_safe_under_concurrent_first_use():
    """Parallel scenarios all resolve tools at once against a cold registry.

    The loaded flag must not be set before the imports finish, or a second thread resolves
    against a half-populated registry and fails with "pattern matched no tools".
    Dropping the modules from sys.modules makes the import inside _ensure_loaded actually
    re-execute, which is what reproduces the race.
    """
    import sys
    import threading as _threading

    from plumbing.tools import registry as reg

    import plumbing.tools as tools_pkg

    short_names = ("comms_tools", "info_tools", "ops_tools")
    module_names = [f"plumbing.tools.{m}" for m in short_names]
    saved = {name: sys.modules[name] for name in module_names if name in sys.modules}
    saved_registry = dict(reg._REGISTRY)

    try:
        # Dropping it from sys.modules is not enough: `from plumbing.tools import X`
        # returns the cached attribute on the package without re-executing the module,
        # so the @tool decorators would never run again.
        for name, short in zip(module_names, short_names):
            sys.modules.pop(name, None)
            if hasattr(tools_pkg, short):
                delattr(tools_pkg, short)
        reg._REGISTRY.clear()
        reg._loaded = False

        errors: list[Exception] = []
        counts: list[int] = []
        barrier = _threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            try:
                counts.append(len(reg.resolve(["clock.*", "rules.*", "crm.*", "calendar.*"])))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [_threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, errors
        assert len(set(counts)) == 1, f"threads disagreed on registry size: {counts}"
        assert counts[0] > 0
    finally:
        reg._REGISTRY.clear()
        reg._REGISTRY.update(saved_registry)
        sys.modules.update(saved)
        for name, short in zip(module_names, short_names):
            if name in saved:
                setattr(tools_pkg, short, saved[name])
        reg._loaded = True


# ======================================================================
# Live-tool gate (real services must be unreachable by default)
# ======================================================================


def test_master_switch_is_off_by_default():
    """Shipping with this on would mean real charges on the first test run."""
    from plumbing.integrations import live_status

    status = live_status()
    assert status["master_switch"] is False
    assert status["effectively_live"] == []


def test_tool_marked_live_still_simulated_while_master_switch_off():
    """Both switches are required. One alone must not reach a real service."""
    from plumbing import config
    from plumbing.integrations import gate

    original = config.tool_catalog
    try:
        config.tool_catalog = lambda: {  # type: ignore[assignment]
            "live_tools_enabled": False,
            "statuses": {"sms.send": "live", "payment.send_deposit_link": "live"},
        }
        assert gate.is_live("sms.send") is False
        assert gate.is_live("payment.send_deposit_link") is False
        assert gate.live_status()["effectively_live"] == []
    finally:
        config.tool_catalog = original  # type: ignore[assignment]


def test_gate_opens_only_when_both_switches_set():
    from plumbing import config
    from plumbing.integrations import gate

    original = config.tool_catalog
    try:
        config.tool_catalog = lambda: {  # type: ignore[assignment]
            "live_tools_enabled": True,
            "statuses": {"sms.send": "live", "email.send": "mocked"},
        }
        assert gate.is_live("sms.send") is True
        assert gate.is_live("email.send") is False       # marked mocked
        assert gate.is_live("payment.refund_deposit") is False  # not listed at all
    finally:
        config.tool_catalog = original  # type: ignore[assignment]


def test_sms_send_stays_in_the_outbox_when_not_live():
    world = make_world()
    result, _ = call(world, "sms.send", to="+16045550101", body="hello", purpose="status_update")
    assert result["ok"]
    assert result.get("live") is not True
    assert world.sms_outbox[0]["live"] is False
    assert "provider_message_id" not in world.sms_outbox[0]


# ======================================================================
# Warranty technician review (the agent must not approve claims itself)
# ======================================================================


def _review_world(verdict: str = "approve", reason: str = "", delay: int = 30) -> World:
    return make_world(
        overrides={
            "warranty_review": {
                "verdict": verdict,
                "reason": reason,
                "response_delay_minutes": delay,
            }
        }
    )


def test_review_request_routes_to_the_original_technician():
    world = _review_world()
    ticket = world.seed_ticket("Warranty Eligibility Review", "+16045550101")
    result, _ = call(
        world,
        "review.request_warranty",
        ticket_id=ticket.ticket_id,
        job_id="J-2025-0412",
        summary="Same joint seeping again",
    )
    assert result["ok"]
    assert result["status"] == "pending"
    # J-2025-0412 was Mike Wang's job, so it must go to him and nobody else
    assert result["technician"]["id"] == "t_wang"


def test_review_verdict_is_pending_until_time_passes():
    world = _review_world(delay=30)
    ticket = world.seed_ticket("Warranty Eligibility Review", "+16045550101")
    requested, _ = call(
        world,
        "review.request_warranty",
        ticket_id=ticket.ticket_id,
        job_id="J-2025-0412",
        summary="Same joint seeping again",
    )
    review_id = requested["review_id"]

    early, _ = call(world, "review.get_verdict", review_id=review_id)
    assert early["status"] == "pending"

    world.advance(45)
    decided, _ = call(world, "review.get_verdict", review_id=review_id)
    assert decided["status"] == "decided"
    assert decided["verdict"] == "approve"


def test_review_verdict_can_reject_with_a_reason():
    world = _review_world(verdict="reject", reason="Separate fitting, not the joint I repaired")
    ticket = world.seed_ticket("Warranty Eligibility Review", "+16045550101")
    requested, _ = call(
        world,
        "review.request_warranty",
        ticket_id=ticket.ticket_id,
        job_id="J-2025-0412",
        summary="Water under the sink again",
    )
    world.advance(60)
    decided, _ = call(world, "review.get_verdict", review_id=requested["review_id"])
    assert decided["verdict"] == "reject"
    assert "Separate fitting" in decided["reason"]


def test_unclear_record_goes_to_the_on_duty_technician_not_a_supervisor():
    """An ambiguous record is a question for a tradesperson, not an escalation."""
    world = _review_world()
    ticket = world.seed_ticket("Warranty Eligibility Review", "+16045550101")
    result, _ = call(
        world,
        "review.request_warranty",
        ticket_id=ticket.ticket_id,
        summary="Customer insists we did this work; nothing matching on file.",
    )
    assert result["ok"]
    assert result["status"] == "pending"
    assert result["routed_to_original_technician"] is False
    assert world.technicians[result["technician"]["id"]].on_duty is True


def test_claim_routes_to_the_original_technician_when_the_job_is_known():
    world = _review_world()
    ticket = world.seed_ticket("Warranty Eligibility Review", "+16045550101")
    result, _ = call(
        world,
        "review.request_warranty",
        ticket_id=ticket.ticket_id,
        job_id="J-2025-0412",
        summary="Same joint seeping again",
    )
    assert result["routed_to_original_technician"] is True
    assert result["technician"]["id"] == "t_wang"


def test_an_off_duty_original_technician_is_replaced_not_blocked():
    """Nobody waits on a claim because the original technician has left."""
    world = make_world(
        overrides={
            "warranty_review": {"verdict": "approve"},
            "technicians": {"t_wang": {"on_duty": False}},
        }
    )
    ticket = world.seed_ticket("Warranty Eligibility Review", "+16045550101")
    result, _ = call(
        world,
        "review.request_warranty",
        ticket_id=ticket.ticket_id,
        job_id="J-2025-0412",
        summary="Same joint seeping again",
    )
    assert result["ok"]
    assert result["technician"]["id"] != "t_wang"
    assert world.technicians[result["technician"]["id"]].on_duty is True


def test_warranty_booking_requires_passing_through_technician_review():
    """Eligibility review cannot jump straight to booked — a human has to rule first."""
    world = make_world()
    ticket = world.seed_ticket("Warranty Eligibility Review", "+16045550101")
    with pytest.raises(ToolRejection) as exc:
        world.transition_ticket(ticket.ticket_id, "Warranty Booked")
    assert "Warranty Technician Review" in str(exc.value)

    world.transition_ticket(ticket.ticket_id, "Warranty Technician Review")
    world.transition_ticket(ticket.ticket_id, "Warranty Booked")
    assert ticket.status == "Warranty Booked"


def test_technician_review_can_route_to_new_paid_work():
    world = make_world()
    ticket = world.seed_ticket("Warranty Eligibility Review", "+16045550101")
    world.transition_ticket(ticket.ticket_id, "Warranty Technician Review")
    world.transition_ticket(ticket.ticket_id, "Needs Assessment")
    assert ticket.status == "Needs Assessment"


# ======================================================================
# Materials collected by email (the address becomes CRM data)
# ======================================================================


def _materials_world(**behavior) -> World:
    return make_world(overrides={"materials": behavior} if behavior else None)


def test_requesting_materials_sends_an_email_and_saves_the_address():
    """The point of collecting by email: the address ends up on the customer's record."""
    world = _materials_world()
    ticket = world.seed_ticket("Needs Assessment", "+16045550101")
    world.customers["+16045550101"].email = ""      # not on file yet

    result, _ = call(
        world,
        "email.request_materials",
        ticket_id=ticket.ticket_id,
        email="linda.new@example.com",
        phone="604-555-0101",
        asking_for="a photo of the joint under the sink",
    )
    assert result["ok"]
    assert result["saved_to_crm"] is True
    assert world.customers["+16045550101"].email == "linda.new@example.com"

    # And an actual email went out, asking them to reply
    sent = world.email_outbox[-1]
    assert sent["purpose"] == "materials_request"
    assert sent["to"] == "linda.new@example.com"
    assert "reply to" in sent["body"].lower()
    assert "photo of the joint under the sink" in sent["body"]


def test_materials_arrive_only_after_the_customer_has_had_time():
    world = _materials_world(reply="sends", reply_delay_minutes=20)
    ticket = world.seed_ticket("Needs Assessment", "+16045550101")
    call(
        world,
        "email.request_materials",
        ticket_id=ticket.ticket_id,
        email="lzhang@example.com",
        asking_for="a photo",
    )

    early, _ = call(world, "email.get_materials", ticket_id=ticket.ticket_id)
    assert early["received"] is False
    assert early["status"] == "pending"

    world.advance(30)
    arrived, _ = call(world, "email.get_materials", ticket_id=ticket.ticket_id)
    assert arrived["received"] is True
    assert arrived["attachments"]
    assert world.snapshot()["received_materials"][0]["from"] == "lzhang@example.com"


def test_customer_who_never_replies_is_reported_not_left_pending():
    """A job must not stall behind a photo, so 'no reply' is a distinct answer."""
    world = _materials_world(reply="none")
    ticket = world.seed_ticket("Needs Assessment", "+16045550101")
    call(
        world,
        "email.request_materials",
        ticket_id=ticket.ticket_id,
        email="lzhang@example.com",
        asking_for="a photo",
    )
    world.advance(600)
    result, _ = call(world, "email.get_materials", ticket_id=ticket.ticket_id)
    assert result["received"] is False
    assert result["status"] == "no_reply"
    assert "stall" in result["note"]


def test_materials_request_rejects_a_malformed_address():
    world = _materials_world()
    ticket = world.seed_ticket("Needs Assessment", "+16045550101")
    result, _ = call(
        world,
        "email.request_materials",
        ticket_id=ticket.ticket_id,
        email="not-an-address",
        asking_for="a photo",
    )
    assert not result["ok"]
    assert "not a valid email" in result["error"]


def test_checking_materials_before_requesting_them_is_rejected():
    world = _materials_world()
    ticket = world.seed_ticket("Needs Assessment", "+16045550101")
    result, _ = call(world, "email.get_materials", ticket_id=ticket.ticket_id)
    assert not result["ok"]
    assert "request_materials first" in result["error"]


def test_upload_tools_are_gone():
    """Materials go by email now; a stale upload tool would be a second, untraceable path."""
    from plumbing.tools import all_tools

    assert not [name for name in all_tools() if name.startswith("upload.")]


def test_a_small_repair_or_an_emergency_never_asks_for_photographs():
    """Somebody is going out to look at those anyway.

    Asking a customer with water on the floor to go and photograph it is worse than
    useless, so small_job and emergency cannot reach these tools at all.
    """
    from plumbing import config
    from plumbing.tools import resolve

    cfg = config.agents_config()
    for agent in ("small_job", "emergency"):
        names = {t.name for t in resolve(cfg["agents"][agent]["tools"])}
        assert "email.request_materials" not in names, agent
        assert "email.get_materials" not in names, agent

    # intake can. A large project is escalated to a person now rather than handed to an
    # agent, and a person cannot price one from a sentence.
    intake = {t.name for t in resolve(cfg["agents"]["intake"]["tools"])}
    assert "email.request_materials" in intake

    for agent in ("warranty", "large_job"):
        names = {t.name for t in resolve(cfg["agents"][agent]["tools"])}
        assert "email.request_materials" in names, agent
        assert "email.get_materials" in names, agent


def test_materials_guidance_only_reaches_the_agents_that_collect():
    """A prompt fragment telling an agent how to collect photos, given to an agent that
    cannot, is an invitation to promise something it has no tool for."""
    from plumbing import agent_registry, config

    cfg = config.agents_config()
    for agent in ("warranty", "large_job", "intake"):
        assert "request_materials" in agent_registry.build_system_prompt(agent, cfg)
    for agent in ("small_job", "emergency"):
        assert "request_materials" not in agent_registry.build_system_prompt(agent, cfg)


# ======================================================================
# Technician outcome, quotes and follow-ups (built, not yet granted)
# ======================================================================


def _attended_world(result: str = "completed", reason: str = "", delay: int = 120) -> tuple:
    world = make_world(overrides={"job_outcome": {"result": result, "reason": reason,
                                                  "delay_minutes": delay}})
    ticket = world.seed_ticket("Appointment Booked", "+16045550101")
    appointment = world.create_appointment(
        kind="standard", ticket_id=ticket.ticket_id, phone="+16045550101",
        start=world.now(), technician_id="t_wang",
        address="5900 No. 3 Rd", description="kitchen sink leak",
    )
    return world, ticket, appointment


def test_job_outcome_is_pending_while_the_technician_is_still_there():
    world, _, appointment = _attended_world(delay=120)
    result, _ = call(world, "technician.get_job_outcome",
                     appointment_id=appointment.appointment_id)
    assert result["status"] == "pending"


def test_technician_reports_the_work_done():
    world, _, appointment = _attended_world(result="completed", delay=60)
    world.advance(90)
    result, _ = call(world, "technician.get_job_outcome",
                     appointment_id=appointment.appointment_id)
    assert result["result"] == "completed"
    assert "thank-you" in result["next_step"]
    # The technician is free again
    assert world.technicians["t_wang"].status == "completed"


def test_technician_reports_the_customer_declined():
    """The other outcome: attended, customer decided against it. Same closing path."""
    world, _, appointment = _attended_world(
        result="declined", reason="customer did not want to pay for the repair", delay=60)
    world.advance(90)
    result, _ = call(world, "technician.get_job_outcome",
                     appointment_id=appointment.appointment_id)
    assert result["result"] == "declined"
    assert "did not want to pay" in result["reason"]
    assert "thank-you" in result["next_step"]


def test_quote_lifecycle_and_followups_cancel_on_a_decision():
    world = make_world()
    ticket = world.seed_ticket("Large Project Under Review", "+16045550104")

    created, _ = call(world, "quote.create", ticket_id=ticket.ticket_id,
                      scope="Second floor repipe, 20 units", amount=48000, duration_days=12)
    assert created["status"] == "draft"

    sent, _ = call(world, "quote.mark_sent", ticket_id=ticket.ticket_id)
    assert sent["status"] == "sent"
    assert sent["followup_hours"] == [24, 48, 72]

    for hours in sent["followup_hours"]:
        call(world, "schedule.create_followup", ticket_id=ticket.ticket_id,
             in_hours=hours, purpose="quote_reminder")
    assert len(world.followups) == 3

    none_due, _ = call(world, "schedule.list_due", ticket_id=ticket.ticket_id)
    assert none_due["count"] == 0

    world.advance(60 * 25)
    due, _ = call(world, "schedule.list_due", ticket_id=ticket.ticket_id)
    assert due["count"] == 1

    # A decision makes any remaining chasing pointless
    decided, _ = call(world, "quote.record_decision", ticket_id=ticket.ticket_id, accepted=True)
    assert decided["status"] == "accepted"
    assert all(f["status"] != "scheduled" for f in world.followups)


def test_quote_rejects_a_nonsense_amount():
    world = make_world()
    ticket = world.seed_ticket("Large Project Under Review", "+16045550104")
    result, _ = call(world, "quote.create", ticket_id=ticket.ticket_id, scope="x", amount=0)
    assert not result["ok"]
    assert "greater than zero" in result["error"]


def test_quote_tools_are_not_granted_yet():
    """Formal quoting belongs to large_job, which is still scaffolding."""
    from plumbing import config
    from plumbing.tools import resolve

    cfg = config.agents_config()
    granted = set()
    for spec in cfg["agents"].values():
        granted |= {t.name for t in resolve(spec["tools"])}

    for name in ("quote.create", "quote.mark_sent", "quote.record_decision"):
        assert name not in granted, f"{name} was wired to an agent before it was meant to be"


def test_agents_that_dispatch_can_wait_for_the_technician():
    """Handing a job over and then checking back a day later needs both tools."""
    from plumbing import config
    from plumbing.tools import resolve

    cfg = config.agents_config()
    for agent in ("warranty", "small_job", "emergency"):
        names = {t.name for t in resolve(cfg["agents"][agent]["tools"])}
        assert "technician.get_job_outcome" in names, agent
        assert "schedule.create_followup" in names, agent

    # intake never puts a job in a technician's hands, so it has no business waiting
    intake = {t.name for t in resolve(cfg["agents"]["intake"]["tools"])}
    assert "technician.get_job_outcome" not in intake


def test_apartment_small_job_is_declined_but_large_project_is_not():
    world = make_world()
    small, _ = call(world, "rules.check_service_eligibility",
                    property_type="apartment", job_size="small_job")
    assert small["can_serve"] is False
    assert "insurance" in small["reason"]

    large, _ = call(world, "rules.check_service_eligibility",
                    property_type="apartment", job_size="large_job")
    assert large["can_serve"] is True
    assert large["requires_human_review"] is True

    house, _ = call(world, "rules.check_service_eligibility",
                    property_type="house", job_size="small_job")
    assert house["can_serve"] is True


# ======================================================================
# The state machine must match the flow the prompts actually run
# ======================================================================


def test_emergency_chain_is_deposit_first():
    """The deposit is what pays to start the search, so it comes before it.

    This is the order the emergency prompt runs; a state machine encoding the old
    find-then-charge order blocks the agent halfway through and it cannot recover.
    """
    world = make_world()
    ticket = world.seed_ticket("Needs Assessment", "+16045550101")
    for status in [
        "Deposit Link Sent",
        "Deposit Paid",
        "Emergency Technician Search",
        "Emergency Technician Confirmed",
        "Emergency Job Dispatched",
    ]:
        world.transition_ticket(ticket.ticket_id, status)
    assert ticket.status == "Emergency Job Dispatched"


def test_search_with_no_taker_can_lead_to_a_refund():
    world = make_world()
    ticket = world.seed_ticket("Deposit Paid", "+16045550101")
    world.transition_ticket(ticket.ticket_id, "Emergency Technician Search")
    world.transition_ticket(ticket.ticket_id, "Refund Pending")
    world.transition_ticket(ticket.ticket_id, "Refund Completed")
    assert ticket.status == "Refund Completed"


def test_small_job_can_book_straight_from_triage():
    """intake hands off at Needs Assessment and small_job books; nothing sits between."""
    world = make_world()
    ticket = world.seed_ticket("Needs Assessment", "+16045550101")
    world.transition_ticket(ticket.ticket_id, "Appointment Booked")
    world.transition_ticket(ticket.ticket_id, "Service Completed")
    assert ticket.status == "Service Completed"


def test_every_prompt_state_reference_is_a_real_state():
    """A prompt naming a state the machine does not have sends the agent into a wall."""
    import re

    from plumbing import agent_registry, config

    cfg = config.agents_config()
    valid = set(config.ticket_states()["states"])
    for agent in cfg["agents"]:
        prompt = agent_registry.build_system_prompt(agent, cfg)
        # Backticked Title Case phrases are how prompts name ticket states
        for quoted in re.findall(r"`([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)+)`", prompt):
            assert quoted in valid, f"{agent} names unknown state {quoted!r}"


# ======================================================================
# Recovery paths through the state machine
#
# The state machine exists to stop the agent skipping steps that matter — taking money
# before confirming, dispatching before payment. It is not there to enforce one canonical
# route, and blocking a legitimate recovery just strands the job.
# ======================================================================


def test_warranty_can_be_raised_mid_assessment():
    """Customers mention warranty when it occurs to them, not only in their first sentence."""
    world = make_world()
    ticket = world.seed_ticket("Needs Assessment", "+16045550101")
    world.transition_ticket(ticket.ticket_id, "Warranty Eligibility Review")
    assert ticket.status == "Warranty Eligibility Review"


def test_an_urgent_approved_warranty_claim_reaches_the_emergency_flow():
    """Covered work that has failed badly still needs someone tonight, and emergency
    takes the deposit first."""
    world = make_world()
    ticket = world.seed_ticket("Warranty Technician Review", "+16045550101")
    world.transition_ticket(ticket.ticket_id, "Deposit Link Sent")
    world.transition_ticket(ticket.ticket_id, "Deposit Paid")
    assert ticket.status == "Deposit Paid"


def test_a_supervisor_can_put_a_job_back_on_its_feet():
    world = make_world()
    ticket = world.seed_ticket("Escalated to Supervisor", "+16045550101")
    world.transition_ticket(ticket.ticket_id, "Deposit Link Sent")
    assert ticket.status == "Deposit Link Sent"


def test_escalation_still_cannot_reach_just_anything():
    """Named resumptions only. If an escalation could reach any state, the machine would
    stop meaning anything — an agent in trouble would simply escalate and carry on."""
    world = make_world()
    ticket = world.seed_ticket("Escalated to Supervisor", "+16045550101")
    with pytest.raises(ToolRejection):
        world.transition_ticket(ticket.ticket_id, "Emergency Job Dispatched")


def test_a_refunded_emergency_can_become_a_normal_booking():
    """"Nobody can come tonight, have your deposit back — shall I book you tomorrow?"
    is the ordinary ending of a failed search, not an exception."""
    world = make_world()
    ticket = world.seed_ticket("Refund Completed", "+16045550101")
    world.transition_ticket(ticket.ticket_id, "Appointment Booked")
    assert ticket.status == "Appointment Booked"


def test_the_gates_that_matter_are_still_shut():
    """Adding recovery edges must not have opened the ones guarding money and dispatch."""
    world = make_world()

    # Cannot dispatch straight from triage, skipping payment
    ticket = world.seed_ticket("Needs Assessment", "+16045550101")
    with pytest.raises(ToolRejection):
        world.transition_ticket(ticket.ticket_id, "Emergency Job Dispatched")

    # Cannot mark a deposit paid without having sent a link
    other = world.seed_ticket("Needs Assessment", "+16045550101")
    with pytest.raises(ToolRejection):
        world.transition_ticket(other.ticket_id, "Deposit Paid")

    # Warranty still cannot be booked without a human ruling on it
    third = world.seed_ticket("Warranty Eligibility Review", "+16045550101")
    with pytest.raises(ToolRejection):
        world.transition_ticket(third.ticket_id, "Warranty Booked")


# ---- reaching a technician --------------------------------------------
#
# It used to be sms.send to their number. The roster carries fictional numbers, Twilio
# answered "Landline or unreachable carrier", nothing raised — and a customer was given a
# confirmation for a job nobody had been told about.


def test_a_technician_is_told_over_telegram():
    world = World(now=WORKDAY)
    world.technicians["t_wang"].telegram_chat_id = "6043701711"

    result, ctx = call(world, "technician.notify", technician_id="t_wang",
                       subject="New job", body="8900 Demorest Dr, today 2pm, Lin")

    assert result["channel"] == "telegram"
    assert result["to"] == world.technicians["t_wang"].name
    assert ctx.world.sms_outbox[-1]["recipient_id"] == "t_wang"
    assert "8900 Demorest Dr" in ctx.world.sms_outbox[-1]["body"]


def test_the_agent_never_names_the_channel():
    """The schema takes a technician and a message, and nothing about how it travels.
    Switching to something else later is a change to the tool, not to five prompts."""
    from plumbing.tools import registry

    registry._ensure_loaded()
    fields = registry.all_tools()["technician.notify"].schema()["function"]["parameters"]
    assert set(fields["properties"]) == {"technician_id", "subject", "body"}


def test_a_technician_nobody_can_reach_is_refused_rather_than_dropped(monkeypatch):
    """Live, with no Telegram for them, there is no way to reach that person at all — and
    the agent must not go on to tell a customer somebody is coming."""
    import plumbing.tools.comms_tools as comms

    world = World(now=WORKDAY)
    world.technicians["t_li"].telegram_chat_id = ""
    monkeypatch.setattr(comms, "is_live", lambda name: name == "telegram.send")

    result, _ = call(world, "technician.notify", technician_id="t_li",
                     subject="New job", body="x")

    assert result["ok"] is False
    assert "no Telegram" in result["error"]
    assert "Do not confirm" in result["error"]
    assert world.sms_outbox == []          # nothing was recorded as sent


def test_an_unknown_technician_is_refused():
    world = World(now=WORKDAY)

    result, _ = call(world, "technician.notify", technician_id="t_nobody",
                     subject="x", body="y")

    assert result["ok"] is False
    assert "t_wang" in result["error"]      # and says who there is instead


def test_small_job_can_reach_a_technician_and_cannot_ring_one():
    """The deployment decided not to pay for calls, and that is enforced by the grant
    rather than by asking the prompt nicely."""
    from plumbing import config
    from plumbing.tools import resolve

    granted = {t.name for t in resolve(config.agents_config()["agents"]["small_job"]["tools"])}

    assert "technician.notify" in granted
    assert not any(name.startswith("phone.") for name in granted)


def test_a_live_job_goes_out_with_accept_and_decline_under_it(monkeypatch, tmp_path):
    """A technician who cannot answer is one the office has to chase. Decline asks why,
    because a refusal with no reason leaves whoever picks it up guessing at what to tell
    the customer."""
    import plumbing.tools.comms_tools as comms
    from plumbing.store import SqliteStore

    sent = {}

    def fake_send(chat_id, text, buttons=None):
        sent.update(chat_id=chat_id, text=text, buttons=buttons)
        return {"message_id": "77"}

    import plumbing.integrations.telegram as tg

    monkeypatch.setattr(tg, "send_message", fake_send)
    monkeypatch.setattr(comms, "is_live", lambda name: name == "telegram.send")
    monkeypatch.setattr("plumbing.live.notify.is_live", lambda name: name == "telegram.send")

    world = World(now=WORKDAY, store=SqliteStore(tmp_path / "d.db"))
    world.technicians["t_wang"].telegram_chat_id = "6043701711"

    result, _ = call(world, "technician.notify", technician_id="t_wang",
                     subject="New job", body="8900 Demorest Dr, today 2pm")

    assert result["sent"] is True and result["offer_id"]
    labels = [b["text"] for row in sent["buttons"] for b in row]
    assert any("Accept" in label for label in labels)
    assert any("Decline" in label for label in labels)
    assert "8900 Demorest Dr" in sent["text"]


def test_without_a_database_it_is_still_sent_just_without_buttons(monkeypatch):
    """Tracking an answer needs somewhere to keep it. The test rig has nowhere, and
    nobody there is going to tap anything."""
    import plumbing.tools.comms_tools as comms
    import plumbing.integrations.telegram as tg

    sent = {}
    monkeypatch.setattr(tg, "send_message",
                        lambda chat_id, text, buttons=None: sent.update(buttons=buttons)
                        or {"message_id": "1"})
    monkeypatch.setattr(comms, "is_live", lambda name: name == "telegram.send")

    world = World(now=WORKDAY)          # no store
    world.technicians["t_wang"].telegram_chat_id = "6043701711"

    result, _ = call(world, "technician.notify", technician_id="t_wang",
                     subject="New job", body="x")

    assert result["sent"] is True
    assert sent["buttons"] is None


def test_a_deployment_can_narrow_the_roster_to_who_really_exists(monkeypatch):
    """Three of the four seeded technicians have no Telegram, so a booking landing on one
    could not be passed to anybody. The seed cannot be trimmed in git — every scenario is
    written against it — so the machine says who is real."""
    monkeypatch.setenv("PLUMBING_ON_DUTY", "t_wang")

    assert sorted(World(now=WORKDAY).technicians) == ["t_wang"]


def test_without_that_the_whole_seeded_roster_is_there():
    """So the suite keeps the world it has always run against."""
    assert len(World(now=WORKDAY).technicians) == 4

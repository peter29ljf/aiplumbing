"""The loop that closes a ticket. No model, no conversation — just the clock.

This is the part of the flow a customer never sees and nobody watches: it runs for days
after the chat is over, and the only evidence it works is that tickets stop piling up. So
it is tested where testing is cheap, and the one behaviour worth being sure of is the one
the old implementation got wrong — it stops asking after two tries.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import followup  # noqa: E402
from bat.runtime import project as projects  # noqa: E402
from bat.runtime.sim import World  # noqa: E402

NOW = "2026-08-05T10:00:00-07:00"


@pytest.fixture()
def world() -> World:
    world = World(now=NOW, rules=projects.find("plumbing").business_rules())
    ticket = world.open_ticket(phone="604-555-0166")
    ticket.tags["issue"] = "dripping tap"
    world.set_status(ticket.id, "Appointment Booked")
    world.followups.append({
        "ticket_id": ticket.id,
        "due": (world.now + timedelta(hours=24)).isoformat(),
        "answered": False,
        "asked": 0,
    })
    return world


def _chases(world: World) -> list[dict]:
    return [m for m in world.technician_messages if m.get("kind") == "followup"]


def _day(world: World, days: int = 1) -> None:
    world.now += timedelta(hours=24 * days)
    followup.tick(world)


def test_nothing_is_asked_before_it_is_due(world: World):
    world.now += timedelta(hours=23)
    followup.tick(world)

    assert _chases(world) == []


def test_the_technician_is_asked_after_a_day(world: World):
    _day(world)

    assert len(_chases(world)) == 1
    assert world.followups[0]["asked"] == 1


def test_it_never_gives_up(world: World):
    """The one that matters. `live/reminders.py` stops at two asks and marks the follow-up
    `given_up` — after which nothing on earth closes that ticket. Silence from a technician
    is not a finished job."""
    for _ in range(6):
        _day(world)

    assert len(_chases(world)) == 6
    assert world.followups[0]["answered"] is False


def test_a_confirmation_closes_the_ticket_and_thanks_the_customer(world: World):
    _day(world)
    ticket_id = world.followups[0]["ticket_id"]

    followup.technician_says(world, ticket_id, done=True)

    assert world.tickets[ticket_id].status == "Closed"
    assert [t for t in world.texts if t.get("kind") == "thanks"]


def test_the_thank_you_finds_a_number_the_model_never_wrote_down(world: World):
    """`ticket.phone` is only set if set_fields happened to carry "phone". The number that
    is reliably there is the one the engine copied off the lookup, and reading only the
    first closed a job with no thank-you at all."""
    ticket = world.tickets[world.followups[0]["ticket_id"]]
    ticket.phone = ""
    ticket.tags["phone"] = "604-555-0166"
    _day(world)

    followup.technician_says(world, ticket.id, done=True)

    assert [t for t in world.texts if t["to"] == "604-555-0166"]


def test_nobody_is_chased_after_they_have_answered(world: World):
    _day(world)
    followup.technician_says(world, world.followups[0]["ticket_id"], done=True)

    for _ in range(3):
        _day(world)

    assert len(_chases(world)) == 1


def test_not_done_keeps_it_open_and_asks_again_tomorrow(world: World):
    _day(world)
    ticket_id = world.followups[0]["ticket_id"]

    followup.technician_says(world, ticket_id, done=False, note="waiting on a part")

    assert world.tickets[ticket_id].status != "Closed"
    _day(world)
    assert len(_chases(world)) == 2


def test_a_ticket_with_no_follow_up_is_left_alone(world: World):
    """A declined job has nobody to chase. The loop must not invent one — a customer who
    was told no does not want a text a day later thanking them for their business."""
    world.followups.clear()
    declined = world.open_ticket(phone="604-555-0177")
    world.set_status(declined.id, "Closed")

    for _ in range(3):
        _day(world)

    assert _chases(world) == []
    assert world.texts == []

"""The follow-up loop has to go out through the world, not around it.

`world.texts` and `world.send_sms()` are indistinguishable in the simulated world: the
method appends to the list, and every assertion in every scenario reads the list. So a
module that appends directly passes the entire suite while doing, in a live world, exactly
nothing — the report shows a chase that the technician's phone never saw and a thank-you
the customer never got.

This one is the whole reason the bug survived: **the record is not the sending.** In the
simulated world the record *is* the sending, which is why nothing else can catch it.

Costs nothing to run. No model, no network, no clock.
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


class WatchedWorld(World):
    """A world that answers to the same names and remembers which were called.

    Deliberately a subclass rather than a stand-in: `LiveWorld` will be one too, and this
    is the same seam. If a module reaches past the method here, it reaches past Twilio
    there.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.called: list[str] = []

    def send_sms(self, to: str, body: str):
        self.called.append("send_sms")
        return super().send_sms(to, body)

    def notify_technician(self, technician_id: str, subject: str, body: str):
        self.called.append("notify_technician")
        return super().notify_technician(technician_id, subject, body)


@pytest.fixture()
def world() -> WatchedWorld:
    world = WatchedWorld(now=NOW, rules=projects.find("plumbing").business_rules())
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


def test_the_chase_goes_through_notify_technician(world: WatchedWorld):
    world.now += timedelta(hours=24)
    followup.tick(world)

    assert world.called == ["notify_technician"]


def test_the_thank_you_goes_through_send_sms(world: WatchedWorld):
    ticket_id = next(iter(world.tickets))
    followup.technician_says(world, ticket_id, done=True)

    assert world.called == ["send_sms"]


def test_every_chase_goes_through_it_not_just_the_first(world: WatchedWorld):
    """Three days, three sends. A loop that reached the method once and the list
    thereafter would chase a technician on day one and nobody after that."""
    for _ in range(3):
        world.now += timedelta(hours=24)
        followup.tick(world)

    assert world.called == ["notify_technician"] * 3


# ---- what the method must not cost us ----------------------------------


def test_the_bookkeeping_still_marks_what_it_wrote(world: WatchedWorld):
    """`kind` is how the report tells a chase from a booking confirmation, and the world's
    own method knows nothing about follow-ups. Going through the method must not lose it."""
    world.now += timedelta(hours=24)
    followup.tick(world)

    assert [m["kind"] for m in world.technician_messages] == ["followup"]


def test_the_module_clock_wins_over_the_world_clock(world: WatchedWorld):
    """`tick` takes a `now` so a loop that runs for days can be tested without waiting
    days. The world stamps `at` from its own clock, so the stamp has to be corrected."""
    later = world.now + timedelta(hours=48)
    followup.tick(world, now=later)

    assert world.technician_messages[-1]["at"] == later.isoformat()

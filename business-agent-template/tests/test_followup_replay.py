"""Asking the technician twice, and not asking him at all.

The follow-up loop is the only part of this system that waits a day and then acts on its
own, which makes it the only part where a crash is invisible: nobody is on the other end
of the conversation to notice that nothing happened. Both ways of getting it wrong are
here, and they are opposites.

**Not asking at all.** `tick()` moved the next due time forward and *then* sent the
message. A process that stopped between the two lost that day's ask entirely and rearmed
itself for tomorrow, so the job sat unchased with the record showing it had been chased.

**Asking twice.** `tick()` writes to the world directly rather than through
`registry.call`, so it had none of the idempotency the tools have. Now that a world can be
saved and restored, replaying a tick from a saved state is not hypothetical — it is what
happens every time a runner restarts.

The fix for both is the same shape: do the outward thing first, record it under a key that
identifies the attempt, and let a replay find the key and do nothing.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import followup  # noqa: E402
from bat.runtime.sim import World  # noqa: E402

RULES = {"company": {"name": "Test", "timezone": "America/Vancouver"}}
NOW = "2026-08-06T10:00:00-07:00"


def _armed() -> World:
    world = World(now=NOW, rules=RULES)
    ticket = world.open_ticket(phone="604-555-0166")
    ticket.tags["issue"] = "dripping tap"
    world.schedule_followup(ticket.id, hours=24)
    return world


def _tomorrow(world: World):
    return world.now + timedelta(hours=25)


def test_the_technician_is_asked_once_when_it_comes_due():
    world = _armed()

    asked = followup.tick(world, _tomorrow(world))

    assert len(asked) == 1
    assert len([m for m in world.technician_messages if m["kind"] == "followup"]) == 1


def test_replaying_the_same_tick_does_not_ask_again():
    """Two runners, or one runner restarted — the same tick against the same state."""
    world = _armed()
    when = _tomorrow(world)

    followup.tick(world, when)
    followup.tick(world, when)

    assert len([m for m in world.technician_messages if m["kind"] == "followup"]) == 1


def test_a_tick_replayed_from_a_saved_world_does_not_ask_again():
    """The case that made this urgent: state now survives a crash, so a restart replays."""
    world = _armed()
    when = _tomorrow(world)
    followup.tick(world, when)

    resumed = World.restore(world.save(), rules=RULES)
    followup.tick(resumed, when)

    assert len([m for m in resumed.technician_messages if m["kind"] == "followup"]) == 1


def test_the_next_day_is_a_different_ask():
    """Idempotency must not turn "never gives up" into "asked once and stopped"."""
    world = _armed()
    followup.tick(world, _tomorrow(world))

    followup.tick(world, world.now + timedelta(hours=49))

    assert len([m for m in world.technician_messages if m["kind"] == "followup"]) == 2


def test_a_stop_part_way_through_a_tick_does_not_lose_the_ask():
    """The one that needed a crash to see, and the reason the order matters.

    `tick` moved the due time forward and then sent the message. Stopping between the two
    — the process killed, the messaging service down — rearmed the follow-up for tomorrow
    with nothing having gone out. The job sat unchased and the record said it had been
    chased, which is worse than either.

    A passing run cannot tell the two orders apart. This one makes the send fail."""
    world = _armed()
    when = _tomorrow(world)

    class Broken(list):
        def append(self, _item):
            raise RuntimeError("the messaging service is down")

    world.technician_messages = Broken()
    try:
        followup.tick(world, when)
    except RuntimeError:
        pass

    entry = world.followups[0]
    assert datetime.fromisoformat(entry["due"]) <= when, (
        "the ask was rearmed for tomorrow having never gone out")
    assert entry.get("asked", 0) == 0, "it was recorded as asked and never was"

    # And on the retry it goes out exactly once.
    world.technician_messages = []
    followup.tick(world, when)
    assert len([m for m in world.technician_messages if m["kind"] == "followup"]) == 1


def test_an_answered_follow_up_is_left_alone():
    world = _armed()
    followup.tick(world, _tomorrow(world))
    ticket_id = world.followups[0]["ticket_id"]

    followup.technician_says(world, ticket_id, done=True, now=_tomorrow(world))
    followup.tick(world, world.now + timedelta(hours=49))

    assert len([m for m in world.technician_messages if m["kind"] == "followup"]) == 1
    assert world.tickets[ticket_id].status == "Closed"


def test_thanking_the_customer_happens_once():
    """`technician_says` is answerable twice — a technician replying "done" twice is a
    person, not a bug — and the customer should not be thanked twice for one job."""
    world = _armed()
    ticket_id = world.followups[0]["ticket_id"]
    when = _tomorrow(world)

    followup.technician_says(world, ticket_id, done=True, now=when)
    followup.technician_says(world, ticket_id, done=True, now=when)

    assert len([t for t in world.texts if t.get("kind") == "thanks"]) == 1

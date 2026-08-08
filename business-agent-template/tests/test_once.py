"""Doing the same irreversible thing twice.

Five of the sixteen tools reach outside the process: they book a slot in a diary, text a
customer, wake a technician, raise an escalation, arm a follow-up. Everything that makes
those run more than once already exists in the system and none of it is exotic — the
closing gate sends a terminal step round again for each tool it has not called, the
`_still_here` nudge sends a step round again for talking without finishing, a customer
who comes back re-enters the graph, and the follow-up loop is designed to run every day
forever. A real run showed `consultant_send_enquiry` called three times inside one step.

For a lookup that is harmless. For a booking it is a second appointment in a diary that a
person then has to go and cancel, and for a text it is the customer's phone going twice.

So the five declare `once=True`, and the second call with the same arguments returns the
first call's answer without doing anything. The caller cannot tell, and should not need
to: that is what idempotency is. What is recorded is the repeat, because a step that has
to be stopped from booking twice is a step worth looking at.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import registry  # noqa: E402
from bat.runtime.sim import World  # noqa: E402

RULES = {
    "company": {"name": "Test", "timezone": "America/Vancouver"},
}


def _world() -> World:
    registry.load_tools(None)
    return World(now="2026-08-06T10:00:00-07:00", rules=RULES)


def _call(world, wire, args, allowed):
    return registry.call(world, wire, args, allowed)


# ---- the point of it ----------------------------------------------------


def test_texting_the_same_person_the_same_thing_twice_sends_one_text():
    world = _world()
    ticket = world.open_ticket()
    args = '{"to": "604-555-0166", "body": "You are booked for Tuesday at 11."}'

    first, _ = _call(world, "sms_send", args, ("sms.send",))
    second, _ = _call(world, "sms_send", args, ("sms.send",))

    assert len(world.texts) == 1, "the customer's phone went twice"
    assert second == first, "the caller could tell, and should not be able to"


def test_a_different_message_to_the_same_person_does_send():
    world = _world()
    world.open_ticket()

    _call(world, "sms_send", '{"to": "604-555-0166", "body": "Booked for Tuesday."}',
          ("sms.send",))
    _call(world, "sms_send", '{"to": "604-555-0166", "body": "Running late, sorry."}',
          ("sms.send",))

    assert len(world.texts) == 2


def test_a_lookup_is_not_held_back():
    """`calendar.find_slots` twice is two honest answers, and the diary may have moved
    between them. Only what reaches outside the process is held."""
    world = _world()

    _call(world, "clock_now", "{}", ("clock.now",))
    result, _ = _call(world, "clock_now", "{}", ("clock.now",))

    assert result, "a lookup was suppressed"


def test_the_repeat_is_recorded_even_though_it_did_nothing():
    """Silently swallowing it would hide the step that needs fixing."""
    world = _world()
    world.open_ticket()
    args = '{"to": "604-555-0166", "body": "Booked."}'

    _call(world, "sms_send", args, ("sms.send",))
    _call(world, "sms_send", args, ("sms.send",))

    assert world.repeats, "nothing recorded the second attempt"
    assert "sms" in world.repeats[0]["tool"]


def test_two_worlds_do_not_share_a_memory():
    """Ten scenarios run at once. One booking a slot must not stop another booking it."""
    one, two = _world(), _world()
    one.open_ticket()
    two.open_ticket()
    args = '{"to": "604-555-0166", "body": "Booked."}'

    _call(one, "sms_send", args, ("sms.send",))
    _call(two, "sms_send", args, ("sms.send",))

    assert len(one.texts) == 1 and len(two.texts) == 1


def test_a_refusal_is_not_remembered_as_done():
    """A tool that refused did nothing, so the next attempt has to be allowed to try —
    otherwise a step that fixes its arguments and calls again gets its own refusal back
    forever."""
    world = _world()
    ticket = world.open_ticket()

    first, _ = _call(world, "sms_send", '{"to": "", "body": "Booked."}', ("sms.send",))
    assert first.get("ok") is False

    second, _ = _call(world, "sms_send", '{"to": "604-555-0166", "body": "Booked."}',
                      ("sms.send",))
    assert second.get("ok") is not False
    assert len(world.texts) == 1

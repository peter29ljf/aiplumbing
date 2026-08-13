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


def _ledger_key(name: str, arguments: str) -> str:
    """The same key `registry.call` builds, built the same way rather than guessed."""
    import json
    return f"{name}:{json.dumps(json.loads(arguments), sort_keys=True, default=str)}"


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


# ---- the crash in the middle --------------------------------------------
#
# Everything above is one process staying alive. The ledger was written after the handler
# returned, which holds for as long as nothing stops in between. Twilio acknowledging a
# message and this process recording that it did are two events with a gap between them,
# and a gap is a place to die. Come back, find nothing recorded, send again.
#
# So the intent goes in first. These three are what that has to mean.


def test_a_crash_between_sending_and_recording_does_not_send_again():
    """The whole reason for the change. The handler reached the outside world and then the
    process died; the world is restored from its last save and the step tries again."""
    world = _world()
    world.open_ticket()
    args = '{"to": "604-555-0166", "body": "Booked for Tuesday at 11."}'

    def explode(_world, **_kwargs):
        _world.texts.append({"to": "604-555-0166", "body": "sent for real"})
        raise RuntimeError("killed after Twilio said yes")

    real = registry._TOOLS["sms.send"]["handler"]
    registry._TOOLS["sms.send"]["handler"] = explode
    try:
        _call(world, "sms_send", args, ("sms.send",))
    except RuntimeError:
        pass
    finally:
        registry._TOOLS["sms.send"]["handler"] = real

    revived = World.restore(world.save(), rules=RULES)
    result, _ = _call(revived, "sms_send", args, ("sms.send",))

    assert len(revived.texts) == 1, "the customer's phone went twice"
    assert result.get("ok") is False


def test_the_step_is_told_it_is_unknown_rather_than_told_it_worked():
    """Handing back a cheerful answer would be worse than sending twice: the step would
    tell the customer it was done, and nobody would ever look."""
    world = _world()
    world.open_ticket()
    args = '{"to": "604-555-0166", "body": "Booked."}'
    world.done[_ledger_key("sms.send", args)] = registry.UNCONFIRMED

    result, _ = _call(world, "sms_send", args, ("sms.send",))

    assert result["ok"] is False
    assert "escalate" in result["error"], "no way out was offered"
    assert world.texts == []


def test_an_unconfirmed_repeat_is_marked_as_one():
    """A repeat that was stopped and a repeat nobody can account for are different things
    to find in a report, and only one of them needs a person."""
    world = _world()
    world.open_ticket()
    args = '{"to": "604-555-0166", "body": "Booked."}'

    _call(world, "sms_send", args, ("sms.send",))
    _call(world, "sms_send", args, ("sms.send",))
    assert world.repeats[-1]["unconfirmed"] is False

    world.done[_ledger_key("sms.send", args)] = registry.UNCONFIRMED
    _call(world, "sms_send", args, ("sms.send",))

    assert world.repeats[-1]["unconfirmed"] is True


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

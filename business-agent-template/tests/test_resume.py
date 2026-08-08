"""A conversation that survives the process it started in.

Until now one lived in memory and nowhere else: kill the console and a half-finished
customer is gone — no ticket, no node, no record that anything was said. For a suite that
is merely untidy. For a business it is the customer who gave you their address, their
problem and their phone number, and has to give them again to somebody who has no idea
they rang.

**This architecture is nearly free of the usual difficulty here.** The ticket is already
the only thing that crosses a node boundary, and a node's messages are already discarded
when it advances — so the whole of what has to survive is the world, the node, and the
ticket id. Nothing is lost by a crash except at most the exchange in progress.

What was missing was one direction of one function: `snapshot()` had no `restore()`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import registry  # noqa: E402
from bat.runtime.sim import World  # noqa: E402

RULES = {"company": {"name": "Test", "timezone": "America/Vancouver"}}
NOW = "2026-08-06T10:00:00-07:00"


def _busy_world() -> World:
    """A world with something of every kind in it."""
    registry.load_tools(None)
    world = World(now=NOW, rules=RULES, records=("enquiries",))
    ticket = world.open_ticket(phone="604-555-0166")
    ticket.tags.update({"name": "Nadia", "issue": "dripping tap", "known_customer": "yes"})
    world.set_status(ticket.id, "Appointment Booked")
    world.send_sms("604-555-0166", "Tuesday at 11 it is.")
    world.escalate(ticket.id, "warranty", "she wants it looked at again")
    world.schedule_followup(ticket.id, hours=24)
    world.record("enquiries", {"to": "Priya"})
    return world


def test_a_world_comes_back_the_same():
    """The assertion view is what a scenario reads, so it is what has to match."""
    world = _busy_world()

    after = World.restore(world.save(), rules=RULES)

    assert after.snapshot() == world.snapshot()


def test_the_ticket_and_its_tags_survive():
    world = World.restore(_busy_world().save(), rules=RULES)
    ticket = next(iter(world.tickets.values()))

    assert ticket.tags["name"] == "Nadia"
    assert ticket.tags["issue"] == "dripping tap"
    assert ticket.status == "Appointment Booked"
    assert ticket.phone == "604-555-0166"


def test_what_went_out_survives():
    world = World.restore(_busy_world().save(), rules=RULES)

    assert len(world.texts) == 1
    assert len(world.escalations) == 1
    assert len(world.followups) == 1
    assert world.snapshot()["enquiries"] == [{"to": "Priya"}]


def test_a_resumed_world_does_not_send_the_same_text_twice():
    """The idempotency ledger is part of the state, and the reason it has to be: a
    conversation resumed after a crash is exactly when a step retries something it has
    already done."""
    world = _busy_world()
    args = '{"to": "604-555-0166", "body": "Tuesday at 11 it is."}'
    registry.call(world, "sms_send", args, ("sms.send",))
    assert len(world.texts) == 2, "the direct send and the tool send are different things"

    resumed = World.restore(world.save(), rules=RULES)
    registry.call(resumed, "sms_send", args, ("sms.send",))

    assert len(resumed.texts) == 2, "the resumed world sent it again"


def test_the_next_id_carries_on_rather_than_starting_over():
    """Two tickets called TK-0001 in one conversation is a record nobody can read."""
    world = _busy_world()

    resumed = World.restore(world.save(), rules=RULES)
    fresh = resumed.open_ticket(phone="604-555-0913")

    assert fresh.id not in {t for t in world.tickets}


def test_the_clock_survives():
    """A conversation resumed tomorrow must not think it is still yesterday — the
    scenario's moment is part of the state, not of the process."""
    world = _busy_world()

    resumed = World.restore(world.save(), rules=RULES)

    assert resumed.now == world.now


# ---- and the conversation on top of it ----------------------------------


def test_a_conversation_picks_up_where_it_stopped():
    """No model anywhere: the graph is walked by hand, saved, and rebuilt."""
    from bat.runtime import project as projects
    from bat.runtime.engine import Conversation
    from bat.runtime.graph import load

    project = projects.find("travel")
    flow = load(project, known_tools=registry.load_tools(project))
    rules = project.business_rules()
    world = World(now="2026-03-10T10:00:00-07:00", rules=rules,
                  records=project.records())
    talk = Conversation(world, None, flow)
    talk.world.tickets[talk.ticket_id].tags.update({"name": "Nadia",
                                                    "destination": "Tokyo"})
    talk._advance("enquiry")

    again = Conversation.resume(talk.save(), None, flow, rules=rules)

    assert again.node.name == talk.node.name
    assert again.ticket_id == talk.ticket_id
    assert again.tags["destination"] == "Tokyo"


def test_resuming_does_not_leave_an_empty_second_ticket():
    """`__init__` opens one because a new conversation needs one. A resumed conversation
    is not new, and the spare would be a record nobody ever reads."""
    from bat.runtime import project as projects
    from bat.runtime.engine import Conversation
    from bat.runtime.graph import load

    project = projects.find("travel")
    flow = load(project, known_tools=registry.load_tools(project))
    rules = project.business_rules()
    talk = Conversation(World(now="2026-03-10T10:00:00-07:00", rules=rules), None, flow)

    again = Conversation.resume(talk.save(), None, flow, rules=rules)

    assert list(again.world.tickets) == [talk.ticket_id]

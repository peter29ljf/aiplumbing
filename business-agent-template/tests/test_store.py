"""What has to outlive the process, and what must not change when it does.

Every scenario the suite runs builds a world, walks a graph, and throws the world away.
That is right for a test — each one wants a clean, known world — and it is the whole of
what this engine could do until now. `grep -rl sqlite3 bat/` returned nothing.

A real customer is the opposite case in every respect. They rang last week. Their visit is
in a diary that must still hold it tomorrow. They come back mid-sentence after a deploy.

So `World` takes an optional `store`, and the rule the rest of this file exists to hold is
**a world without one behaves exactly as it always did**. Two hundred and thirty-odd tests
depend on that and none of them were changed.

No model. sqlite in a tmp_path.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.live.store import SqliteStore  # noqa: E402
from bat.runtime import project as projects  # noqa: E402
from bat.runtime.sim import World  # noqa: E402

NOW = "2026-08-05T10:00:00-07:00"
LATER = "2026-08-06T09:00:00-07:00"


@pytest.fixture()
def rules() -> dict:
    return projects.find("plumbing").business_rules()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "plumbing.db"


def _world(rules: dict, db: Path | None = None, *, now: str = NOW) -> World:
    return World(now=now, rules=rules,
                 store=SqliteStore(db) if db is not None else None)


# ---- the rule everything else rests on ----------------------------------


def test_a_world_without_a_store_is_the_world_it_always_was(rules: dict):
    """The 230 tests that pass no store are the specification, and this says so out loud."""
    world = _world(rules)

    assert world.store is None
    assert world.open_ticket().id == "TK-0001", "ids came from somewhere else"
    assert world.find_customer("604-555-0166") is None


# ---- what a returning customer needs ------------------------------------


def test_somebody_who_rang_last_week_is_found(rules: dict, db: Path):
    """Not "found in this conversation" — found in a process that has since exited."""
    first = _world(rules, db)
    first.add_customer("604-721-8629", name="Dana", address="12 Elm St, Burnaby")

    second = _world(rules, db, now=LATER)

    found = second.find_customer("+1 (604) 721-8629")
    assert found is not None and found.name == "Dana"
    assert found.address == "12 Elm St, Burnaby"


def test_the_visit_they_booked_is_still_theirs(rules: dict, db: Path):
    """`find_appointments` matches on the number rather than the ticket, precisely so this
    works — somebody ringing to move last week's visit is on a new ticket."""
    first = _world(rules, db)
    ticket = first.open_ticket(phone="604-721-8629")
    first.book(ticket_id=ticket.id, starts=first.now + timedelta(days=1), minutes=60,
               technician="t_wang", address="12 Elm St", what="dripping tap",
               phone="604-721-8629")

    second = _world(rules, db, now=LATER)

    theirs = second.find_appointments("6047218629")
    assert [a.what for a in theirs] == ["dripping tap"]


def test_two_customers_are_not_offered_the_same_hour(rules: dict, db: Path):
    """Two people on the widget at once. Each world holds only its own booking in memory,
    so without the store the second is offered a slot the first has just taken."""
    first = _world(rules, db)
    taken = first.free_slots(limit=1)[0]
    ticket = first.open_ticket(phone="604-111-1111")
    first.book(ticket_id=ticket.id, starts=taken, minutes=60, technician="t_wang",
               address="1 A St", what="tap", phone="604-111-1111")

    second = _world(rules, db)

    assert taken not in second.free_slots(limit=6)


def test_ticket_numbers_do_not_start_again_at_one(rules: dict, db: Path):
    """The in-memory counter restarts every process. In production that hands two
    different customers the same ticket number."""
    first = _world(rules, db)
    first.open_ticket()

    assert _world(rules, db, now=LATER).open_ticket().id == "TK-0002"


# ---- a conversation coming back -----------------------------------------


def test_a_resumed_world_keeps_its_store(rules: dict, db: Path):
    """`restore` used to build a plain simulated world whatever it was restoring. A live
    conversation that came back that way looks right and reaches nobody: the next booking
    it makes is written to memory that is thrown away."""
    live = _world(rules, db)
    live.add_customer("604-721-8629", name="Dana")
    state = live.save()

    back = World.restore(state, rules=rules, store=SqliteStore(db))

    assert back.store is not None
    ticket = back.open_ticket(phone="604-721-8629")
    back.book(ticket_id=ticket.id, starts=back.now + timedelta(days=1), minutes=60,
              technician="t_wang", address="12 Elm St", what="tap",
              phone="604-721-8629")

    assert _world(rules, db, now=LATER).find_appointments("604-721-8629")


def test_the_store_is_not_written_into_the_saved_state(rules: dict, db: Path):
    """It is a database connection, not a fact about the world. Saving it would make the
    record unloadable and, worse, make it look loadable."""
    assert "store" not in _world(rules, db).save()


# ---- the ledger that survives a restart ---------------------------------


def test_an_attempt_with_no_answer_is_findable_afterwards(rules: dict, db: Path):
    """The row goes in before the call and the answer overwrites it. One still reading the
    marker is a text that may have gone out with nobody knowing — a person reads this
    list, and nothing is ever sent again on the strength of it."""
    from bat.runtime.registry import UNCONFIRMED

    store = SqliteStore(db)
    store.note_intent("sess-1", "sms.send:{}", UNCONFIRMED)
    store.note_intent("sess-1", "calendar.create:{}", {"booked": True})

    assert [r["key"] for r in store.unconfirmed(UNCONFIRMED)] == ["sms.send:{}"]


def test_a_refused_call_leaves_nothing_behind(rules: dict, db: Path):
    """It did not happen, so the next attempt has to be allowed to try."""
    store = SqliteStore(db)
    store.note_intent("sess-1", "sms.send:{}", "__unconfirmed__")
    store.forget_intent("sess-1", "sms.send:{}")

    assert store.ledger("sess-1") == {}


# ---- the conversation itself --------------------------------------------


def test_a_conversation_can_be_taken_off_the_shelf(rules: dict, db: Path):
    store = SqliteStore(db)
    store.save_conversation("sess-1", {"node": "offer_options", "world": {"now": NOW}},
                            phone="604-721-8629", node="offer_options")

    assert store.load_conversation("sess-1")["node"] == "offer_options"
    assert store.load_conversation("nobody") is None

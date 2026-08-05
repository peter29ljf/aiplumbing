"""The durable store: does a customer survive the conversation that created them?"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.store import SqliteStore, phone_key  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "test.db")


# ---- identity --------------------------------------------------------


def test_the_same_customer_is_found_however_the_number_is_written():
    """People give a number a different way every time, and it is still them."""
    written = ["+1 (604) 721-8629", "604-721-8629", "6047218629", "(604) 721 8629"]
    assert len({phone_key(p) for p in written}) == 1


def test_a_partial_save_does_not_wipe_what_is_already_known(store: SqliteStore):
    """An agent that only asked for a name must not erase last week's address."""
    store.upsert_customer("+16047218629", name="Linda Zhang", address="5900 No. 3 Rd")
    store.upsert_customer("604-721-8629", email="lz@example.com")

    found = store.find_customer("6047218629")
    assert found["name"] == "Linda Zhang"
    assert found["address"] == "5900 No. 3 Rd"
    assert found["email"] == "lz@example.com"


def test_an_unknown_number_is_simply_not_found(store: SqliteStore):
    assert store.find_customer("+16045550000") is None
    assert store.find_customer("") is None


# ---- tickets ---------------------------------------------------------


def test_an_open_ticket_is_found_even_when_the_number_was_stored_differently(store: SqliteStore):
    """Regression: this returned nothing at all.

    Tickets were joined to customers on the raw phone string. The ticket carried
    "+16047218629" and the customer row held "+1 (604) 721-8629", so the join matched
    nothing and every open ticket disappeared — silently, which is the worst way for a
    lookup to fail. Every table holding a phone number now carries the key.
    """
    store.upsert_customer("+1 (604) 721-8629", name="Linda Zhang")
    store.save_ticket({"ticket_id": "TK-0001", "customer_phone": "+16047218629",
                       "status": "Needs Assessment", "tags": {}, "history": []})

    open_now = store.open_tickets("604 721 8629")
    assert [t["ticket_id"] for t in open_now] == ["TK-0001"]


def test_a_closed_ticket_stops_being_open(store: SqliteStore):
    store.save_ticket({"ticket_id": "TK-0001", "customer_phone": "+16047218629",
                       "status": "Needs Assessment", "tags": {}, "history": []})
    store.save_ticket({"ticket_id": "TK-0001", "customer_phone": "+16047218629",
                       "status": "Closed", "tags": {}, "history": []})
    assert store.open_tickets("+16047218629") == []


def test_the_tags_the_apartment_gate_reads_survive_a_round_trip(store: SqliteStore):
    """The gate reads property_type and category off the ticket, so they must persist."""
    store.save_ticket({"ticket_id": "TK-0001", "customer_phone": "+16047218629",
                       "status": "Needs Assessment",
                       "tags": {"property_type": "apartment", "category": "small_job"},
                       "history": [{"at": "t", "status": "Needs Assessment"}]})
    ticket = store.open_tickets("+16047218629")[0]
    assert ticket["tags"]["property_type"] == "apartment"
    assert ticket["history"][0]["status"] == "Needs Assessment"


# ---- ids -------------------------------------------------------------


def test_ticket_numbers_keep_counting_across_restarts(tmp_path: Path):
    """The in-memory counter restarts at 1 each process and would reissue TK-0001."""
    path = tmp_path / "t.db"
    first = SqliteStore(path)
    assert first.next_id("TK") == "TK-0001"
    first.save_ticket({"ticket_id": "TK-0001", "customer_phone": "", "status": "New Inquiry",
                       "tags": {}, "history": []})

    restarted = SqliteStore(path)
    assert restarted.next_id("TK") == "TK-0002"


# ---- appointments ----------------------------------------------------


def test_a_booking_is_still_in_the_diary_tomorrow(store: SqliteStore):
    start = datetime.now().astimezone() + timedelta(days=1)
    store.save_appointment({"appointment_id": "AP-0001", "kind": "standard",
                            "ticket_id": "TK-0001", "customer_phone": "+16047218629",
                            "technician_id": "t_wang", "start": start,
                            "duration_minutes": 120, "address": "5900 No. 3 Rd",
                            "description": "dripping tap"})
    found = store.appointments_between(start - timedelta(hours=1), start + timedelta(hours=1))
    assert [a["appointment_id"] for a in found] == ["AP-0001"]
    assert found[0]["technician_id"] == "t_wang"


def test_a_cancelled_booking_stops_blocking_the_slot(store: SqliteStore):
    start = datetime.now().astimezone() + timedelta(days=1)
    booking = {"appointment_id": "AP-0001", "kind": "standard", "ticket_id": "TK-0001",
               "customer_phone": "+16047218629", "technician_id": "t_wang", "start": start,
               "duration_minutes": 120, "address": "x", "description": "y"}
    store.save_appointment(booking)
    store.save_appointment({**booking, "status": "cancelled"})
    assert store.appointments_between(start - timedelta(hours=1), start + timedelta(hours=1)) == []


def test_the_calendar_event_id_is_not_lost_by_a_later_save(store: SqliteStore):
    """A reschedule must not orphan the real calendar entry it is meant to move."""
    start = datetime.now().astimezone() + timedelta(days=1)
    booking = {"appointment_id": "AP-0001", "kind": "standard", "ticket_id": "TK-0001",
               "customer_phone": "+1604", "technician_id": None, "start": start,
               "duration_minutes": 120, "address": "x", "description": "y"}
    store.save_appointment(booking, calendar_event_id="gcal-abc")
    store.save_appointment({**booking, "status": "rescheduled"})

    with store.connect() as conn:
        row = conn.execute("SELECT calendar_event_id FROM appointments").fetchone()
    assert row["calendar_event_id"] == "gcal-abc"


# ---- conversation and audit ------------------------------------------


def test_one_customer_has_one_conversation_across_channels(store: SqliteStore):
    """They text, then use the web chat. It is the same customer's information."""
    store.add_message(channel="sms", speaker="customer", text="tap is dripping",
                      phone="+1-604-721-8629")
    store.add_message(channel="chat", speaker="agent", text="I can book that",
                      phone="6047218629")
    history = store.conversation(phone="(604) 721 8629")
    assert [m["channel"] for m in history] == ["sms", "chat"]


def test_a_chat_visitor_without_a_number_is_tracked_by_session(store: SqliteStore):
    """Web chat carries no phone number of its own — that is its whole difficulty."""
    store.add_message(channel="chat", speaker="customer", text="do you cover Surrey?",
                      session_id="sess-1")
    assert [m["text"] for m in store.conversation(session_id="sess-1")] == ["do you cover Surrey?"]
    assert store.conversation(session_id="sess-2") == []


def test_events_are_append_only_and_keep_their_order(store: SqliteStore):
    store.add_event("ticket_created", ticket_id="TK-0001")
    store.add_event("apartment_declined", ticket_id="TK-0001", detail="strata", unit="1204")
    events = store.events("TK-0001")
    assert [e["kind"] for e in events] == ["ticket_created", "apartment_declined"]
    assert events[1]["payload"]["unit"] == "1204"


# ---- World with a store ----------------------------------------------


def _empty_world(store, now="2026-08-05T10:00:00-07:00"):
    from plumbing.world import World

    return World(now, seed={"technicians": [], "customers": []}, store=store)


def test_a_customer_is_recognised_when_they_call_back_days_later(tmp_path: Path):
    """The whole reason the store exists. Two Worlds, one database."""
    from plumbing.world import Customer

    path = tmp_path / "prod.db"
    first = _empty_world(SqliteStore(path))
    first.save_customer(Customer(phone="+16047218629", name="Linda Zhang",
                                 address="5900 No. 3 Rd", is_new=True))

    later = _empty_world(SqliteStore(path), now="2026-08-09T09:00:00-07:00")
    found = later.find_customer("604-721-8629")
    assert found is not None and found.name == "Linda Zhang"
    assert found.address == "5900 No. 3 Rd"


def test_a_ticket_is_still_open_the_next_day(tmp_path: Path):
    path = tmp_path / "prod.db"
    first = _empty_world(SqliteStore(path))
    ticket = first.create_ticket("+16047218629")
    first.transition_ticket(ticket.ticket_id, "Phone Verified")

    store = SqliteStore(path)
    still_open = store.open_tickets("6047218629")
    assert [(t["ticket_id"], t["status"]) for t in still_open] == [(ticket.ticket_id, "Phone Verified")]


def test_ticket_numbers_do_not_restart_in_a_new_process(tmp_path: Path):
    """Two customers must never be handed the same ticket number."""
    path = tmp_path / "prod.db"
    assert _empty_world(SqliteStore(path)).create_ticket("+16045550001").ticket_id == "TK-0001"
    assert _empty_world(SqliteStore(path)).create_ticket("+16045550002").ticket_id == "TK-0002"


def test_every_status_change_lands_in_the_audit_log(tmp_path: Path):
    """When a booking is wrong the question is what happened in what order."""
    store = SqliteStore(tmp_path / "prod.db")
    world = _empty_world(store)
    ticket = world.create_ticket("+16047218629")
    world.transition_ticket(ticket.ticket_id, "Phone Verified")
    world.transition_ticket(ticket.ticket_id, "Customer Identified")

    kinds = [e["kind"] for e in store.events(ticket.ticket_id)]
    assert kinds == ["ticket_created", "status_changed", "status_changed"]


def test_a_world_without_a_store_is_untouched(tmp_path: Path):
    """The no-store path is what all the scenarios run against; it must not change."""
    world = _empty_world(None)
    ticket = world.create_ticket("+16047218629")
    assert ticket.ticket_id == "TK-0001"
    assert world.find_customer("+16047218629") is None


# ---- the real calendar -----------------------------------------------


def _book_via_tool(world, **over):
    from plumbing.tools.ops_tools import calendar_create
    from plumbing.tools.registry import ToolContext

    ticket = world.create_ticket("+16047218629")
    args = {"ticket_id": ticket.ticket_id, "kind": "standard", "phone": "+16047218629",
            "start": "2026-08-05T14:00:00-07:00", "address": "5900 No. 3 Rd",
            "description": "dripping tap", "technician_id": ""}
    return calendar_create(ToolContext(world=world), **{**args, **over})


def test_a_calendar_failure_does_not_leave_a_booking_nobody_can_see(tmp_path, monkeypatch):
    """"You are booked" over an entry no technician has is worse than "I could not book"."""
    from plumbing.integrations.gate import LiveToolUnavailable
    from plumbing.world import ToolRejection
    from plumbing.tools import ops_tools

    store = SqliteStore(tmp_path / "prod.db")
    world = _empty_world(store)
    world.technicians["t_wang"] = _a_technician()

    monkeypatch.setattr(ops_tools, "is_live", lambda name: name == "calendar.create_appointment")
    import plumbing.integrations.google_calendar as gcal

    def _boom(**_kwargs):
        raise LiveToolUnavailable("calendar quota exceeded")

    monkeypatch.setattr(gcal, "create_event", _boom)

    with pytest.raises(ToolRejection) as caught:
        _book_via_tool(world, technician_id="t_wang")
    assert "not confirmed" in str(caught.value)
    assert "Do not tell the customer it is booked" in str(caught.value)

    # And the slot is free again, rather than held by a booking that does not exist.
    start = datetime.fromisoformat("2026-08-05T14:00:00-07:00")
    assert store.appointments_between(start - timedelta(hours=1), start + timedelta(hours=1)) == []


def test_a_successful_booking_stores_the_event_id(tmp_path, monkeypatch):
    """Without it a reschedule cannot move the entry and a cancellation cannot remove it."""
    from plumbing.tools import ops_tools
    import plumbing.integrations.google_calendar as gcal

    store = SqliteStore(tmp_path / "prod.db")
    world = _empty_world(store)
    world.technicians["t_wang"] = _a_technician()

    monkeypatch.setattr(ops_tools, "is_live", lambda name: name == "calendar.create_appointment")
    monkeypatch.setattr(gcal, "create_event", lambda **_kw: "gcal-xyz")

    _book_via_tool(world, technician_id="t_wang")
    with store.connect() as conn:
        row = conn.execute("SELECT calendar_event_id FROM appointments").fetchone()
    assert row["calendar_event_id"] == "gcal-xyz"


def test_nothing_reaches_google_while_the_gate_is_shut(tmp_path, monkeypatch):
    """The default. Every tool is mocked until both switches are deliberately turned on."""
    import plumbing.integrations.google_calendar as gcal

    def _should_not_run(**_kwargs):
        raise AssertionError("the real calendar was called with the gate shut")

    monkeypatch.setattr(gcal, "create_event", _should_not_run)

    world = _empty_world(SqliteStore(tmp_path / "prod.db"))
    world.technicians["t_wang"] = _a_technician()
    assert _book_via_tool(world, technician_id="t_wang")["appointment_id"]


def _a_technician():
    from plumbing.world import Technician

    return Technician(id="t_wang", name="Mike Wang", phone="+16045550201",
                      skills=["leak"], areas=["richmond"], max_concurrent_jobs=3,
                      on_duty=True, policy="accept")


# ---- the diary the slot search must respect --------------------------


def test_a_slot_booked_in_an_earlier_conversation_is_not_offered_again(tmp_path: Path):
    """Without this every new conversation believes the diary is empty and resells it."""
    from datetime import datetime as dt

    store = SqliteStore(tmp_path / "prod.db")
    world = _empty_world(store, now="2026-08-05T08:00:00-07:00")
    world.technicians["t_wang"] = _a_technician()

    taken = dt.fromisoformat("2026-08-05T09:00:00-07:00")
    store.save_appointment({"appointment_id": "AP-0001", "kind": "standard",
                            "ticket_id": "TK-0001", "customer_phone": "+1604",
                            "technician_id": "t_wang", "start": taken,
                            "duration_minutes": 120, "address": "x", "description": "y"})

    offered = [s["start"] for s in world.find_slots(limit=5)]
    assert offered, "the search returned nothing at all"
    assert not any(s.startswith("2026-08-05T09:00") for s in offered)
    assert not any(s.startswith("2026-08-05T10:00") for s in offered)   # inside the two hours


def test_a_cancelled_appointment_frees_its_slot_again(tmp_path: Path):
    from datetime import datetime as dt

    store = SqliteStore(tmp_path / "prod.db")
    world = _empty_world(store, now="2026-08-05T08:00:00-07:00")
    world.technicians["t_wang"] = _a_technician()

    booking = {"appointment_id": "AP-0001", "kind": "standard", "ticket_id": "TK-0001",
               "customer_phone": "+1604", "technician_id": "t_wang",
               "start": dt.fromisoformat("2026-08-05T09:00:00-07:00"),
               "duration_minutes": 120, "address": "x", "description": "y"}
    store.save_appointment(booking)
    store.save_appointment({**booking, "status": "cancelled"})

    assert any(s["start"].startswith("2026-08-05T09:00") for s in world.find_slots(limit=5))


def test_what_a_technician_put_in_their_own_calendar_blocks_the_slot(tmp_path, monkeypatch):
    """The appointment nobody here knows about — blocked out by hand, on a phone."""
    from datetime import datetime as dt
    import plumbing.integrations as integrations
    import plumbing.integrations.google_calendar as gcal

    world = _empty_world(SqliteStore(tmp_path / "prod.db"), now="2026-08-05T08:00:00-07:00")
    world.technicians["t_wang"] = _a_technician()

    monkeypatch.setattr(integrations, "is_live", lambda name: name == "calendar.find_slots")
    monkeypatch.setattr(
        gcal, "busy_periods",
        lambda start, end: [(dt.fromisoformat("2026-08-05T09:00:00-07:00"),
                             dt.fromisoformat("2026-08-05T12:00:00-07:00"))],
    )

    offered = [s["start"] for s in world.find_slots(limit=5)]
    assert not any("2026-08-05T09" in s or "2026-08-05T10" in s or "2026-08-05T11" in s
                   for s in offered)


def test_an_unreadable_calendar_refuses_rather_than_guessing(tmp_path, monkeypatch):
    """Offering a time without knowing the diary is how two jobs land on one hour."""
    import plumbing.integrations as integrations
    import plumbing.integrations.google_calendar as gcal
    from plumbing.integrations.gate import LiveToolUnavailable
    from plumbing.world import ToolRejection

    world = _empty_world(SqliteStore(tmp_path / "prod.db"), now="2026-08-05T08:00:00-07:00")
    world.technicians["t_wang"] = _a_technician()

    monkeypatch.setattr(integrations, "is_live", lambda name: name == "calendar.find_slots")

    def _down(start, end):
        raise LiveToolUnavailable("calendar API unreachable")

    monkeypatch.setattr(gcal, "busy_periods", _down)

    with pytest.raises(ToolRejection) as caught:
        world.find_slots(limit=3)
    assert "Do not offer a time" in str(caught.value)


def test_the_calendar_is_read_once_per_search_not_once_per_slot(tmp_path, monkeypatch):
    """A fortnight of hourly candidates would otherwise be hundreds of API calls."""
    import plumbing.integrations as integrations
    import plumbing.integrations.google_calendar as gcal

    world = _empty_world(SqliteStore(tmp_path / "prod.db"), now="2026-08-05T08:00:00-07:00")
    world.technicians["t_wang"] = _a_technician()

    calls = []
    monkeypatch.setattr(integrations, "is_live", lambda name: name == "calendar.find_slots")
    monkeypatch.setattr(gcal, "busy_periods",
                        lambda start, end: calls.append((start, end)) or [])

    world.find_slots(limit=3)
    assert len(calls) == 1

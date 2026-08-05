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

"""The world that reaches real people, with the real people replaced.

Every adapter is swapped for a recorder here, so this suite sends nothing and costs
nothing. What it checks is the handful of decisions that cannot be checked any other way
short of texting a customer:

- **live is an overlay.** The record a scenario asserts on is written in both modes, so a
  suite that passes mocked means the same thing live.
- **a failure means it did not happen**, said in words the step can act on. The alternative
  — a swallowed exception — is a customer told a technician is coming.
- **a booking that Google refused is not a booking.** The internal appointment is undone,
  because an agent must never say "you're booked" over an entry no technician can see.
- **the ledger outlives the process.** A text already sent is not sent again by the process
  that comes back after the one that sent it died.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.live.integrations import gate  # noqa: E402
from bat.live.integrations.gate import LiveToolUnavailable  # noqa: E402
from bat.live.store import SqliteStore  # noqa: E402
from bat.live.world import LiveWorld  # noqa: E402
from bat.runtime import project as projects  # noqa: E402
from bat.runtime import registry  # noqa: E402
from bat.runtime.world import Refused  # noqa: E402

NOW = "2026-08-05T10:00:00-07:00"


@pytest.fixture()
def rules() -> dict:
    return projects.find("plumbing").business_rules()


@pytest.fixture(autouse=True)
def nothing_live(monkeypatch):
    """Both switches set explicitly, so no test can read the real `.env` by accident."""
    monkeypatch.setenv(gate.ENV_MASTER, "false")
    monkeypatch.setenv(gate.ENV_TOOLS, "")


@pytest.fixture()
def live(monkeypatch):
    """Turn everything on. The adapters are still fakes."""
    def switch_on(*names: str) -> None:
        monkeypatch.setenv(gate.ENV_MASTER, "true")
        monkeypatch.setenv(gate.ENV_TOOLS, ",".join(names or sorted(gate.KNOWN_TOOLS)))
    return switch_on


@pytest.fixture()
def world(rules: dict, tmp_path: Path) -> LiveWorld:
    return LiveWorld(now=NOW, rules=rules, store=SqliteStore(tmp_path / "t.db"),
                     session_id="sess-1", supervisor="9999")


class Recorder:
    """Stands in for an adapter. Answers like one, or fails like one."""

    def __init__(self, answer: dict | str, fails: bool = False) -> None:
        self.answer, self.fails, self.calls = answer, fails, []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fails:
            raise LiveToolUnavailable("the service is unreachable")
        return self.answer


def _fake(monkeypatch, module: str, name: str, recorder: Recorder) -> Recorder:
    monkeypatch.setattr(f"bat.live.integrations.{module}.{name}", recorder,
                        raising=False)
    return recorder


# ---- nothing switched on ------------------------------------------------


def test_with_everything_off_it_is_the_simulated_world(world: LiveWorld):
    """The acceptance gate for the whole port: `PLUMBING_LIVE_TOOLS=""` has to behave
    exactly as the suite has always behaved."""
    world.open_ticket(phone="604-721-8629")

    assert world.send_sms("604-721-8629", "Booked.") == {"sent": True,
                                                         "to": "604-721-8629"}
    assert len(world.texts) == 1
    assert "live" not in world.texts[0]


# ---- live is an overlay -------------------------------------------------


def test_a_real_text_is_still_recorded_where_the_assertions_look(world, live, monkeypatch):
    """If live wrote somewhere else, a suite that passes mocked would prove nothing."""
    live("sms.send")
    sent = _fake(monkeypatch, "twilio_sms", "send_sms",
                 Recorder({"provider": "twilio", "message_id": "SM123"}))

    answer = world.send_sms("604-721-8629", "Booked for Tuesday at 11.")

    assert sent.calls == [(("604-721-8629", "Booked for Tuesday at 11."), {})]
    assert len(world.texts) == 1
    assert world.texts[0]["live"] is True
    assert world.texts[0]["provider_message_id"] == "SM123"
    assert answer["live"] is True


def test_the_technician_is_reached_on_the_id_the_roster_holds(world, live, monkeypatch):
    live("telegram.send")
    sent = _fake(monkeypatch, "telegram", "send_message",
                 Recorder({"provider": "telegram", "message_id": "42"}))

    world.notify_technician("t_wang", "New job", "Dripping tap, Burnaby")

    (chat_id, text), _ = sent.calls[0]
    assert chat_id == world.technicians["t_wang"].telegram
    assert "Dripping tap" in text


def test_an_escalation_goes_to_the_supervisor(world, live, monkeypatch):
    live("telegram.send")
    sent = _fake(monkeypatch, "telegram", "send_message",
                 Recorder({"provider": "telegram", "message_id": "43"}))
    ticket = world.open_ticket(phone="604-721-8629")
    ticket.tags["issue"] = "burst pipe"

    world.escalate(ticket.id, "somebody who needs help now", "water everywhere")

    (chat_id, text), _ = sent.calls[0]
    assert chat_id == "9999"
    assert "burst pipe" in text, "the ticket's own facts did not go with it"
    assert len(world.escalations) == 1


# ---- a failure is not a success -----------------------------------------


def test_a_text_that_did_not_send_says_so(world, live, monkeypatch):
    live("sms.send")
    _fake(monkeypatch, "twilio_sms", "send_sms", Recorder({}, fails=True))

    with pytest.raises(Refused) as refused:
        world.send_sms("604-721-8629", "Booked.")

    assert "not tell anyone" in str(refused.value).lower()
    assert world.texts == [], "a text nobody sent was recorded as sent"


def test_a_calendar_that_will_not_answer_does_not_read_as_an_empty_diary(
        world, live, monkeypatch):
    """The dangerous shape. "No busy periods" and "I could not ask" look identical to a
    step, and only one of them means the whole week is free."""
    live("calendar.find_slots")
    _fake(monkeypatch, "google_calendar", "busy_periods", Recorder([], fails=True))

    with pytest.raises(Refused) as refused:
        world.free_slots()

    assert "do not offer a time" in str(refused.value).lower()


def test_a_booking_google_refused_is_not_a_booking(world, live, monkeypatch):
    """The one that matters most. A technician cannot see an entry that does not exist,
    and a customer told they have a slot will be waiting in for nobody."""
    live("calendar.create_appointment")
    _fake(monkeypatch, "google_calendar", "create_event", Recorder("", fails=True))
    ticket = world.open_ticket(phone="604-721-8629")

    with pytest.raises(Refused) as refused:
        world.book(ticket_id=ticket.id, starts=world.now + timedelta(days=1), minutes=60,
                   technician="t_wang", address="12 Elm St", what="tap",
                   phone="604-721-8629")

    assert "not booked" in str(refused.value).lower()
    assert world.appointments == {}, "the internal booking was left standing"
    assert world.find_appointments("604-721-8629") == []


def test_a_booking_that_worked_keeps_the_id_google_gave_it(world, live, monkeypatch):
    """Without it the event exists and nothing here knows what it is called, so the visit
    can never be moved or cancelled again."""
    live("calendar.create_appointment")
    _fake(monkeypatch, "google_calendar", "create_event", Recorder("goog-abc"))
    ticket = world.open_ticket(phone="604-721-8629")

    appointment = world.book(ticket_id=ticket.id, starts=world.now + timedelta(days=1),
                             minutes=60, technician="t_wang", address="12 Elm St",
                             what="tap", phone="604-721-8629")

    assert world.store.calendar_event_id(appointment.id) == "goog-abc"


def test_a_technician_with_no_telegram_id_is_a_refusal_not_a_silence(
        world, live, monkeypatch):
    live("telegram.send")
    world.technicians["t_wang"].telegram = ""

    with pytest.raises(Refused) as refused:
        world.notify_technician("t_wang", "New job", "details")

    assert "escalate" in str(refused.value)


# ---- the ledger that outlives the process -------------------------------


def test_a_text_already_sent_is_not_sent_again_by_the_next_process(
        rules: dict, tmp_path: Path, live, monkeypatch):
    """The crash window. The in-memory ledger is empty in a new process, so without a
    durable one the customer's phone goes twice."""
    live("sms.send")
    sent = _fake(monkeypatch, "twilio_sms", "send_sms",
                 Recorder({"provider": "twilio", "message_id": "SM1"}))
    registry.load_tools(None)
    db = tmp_path / "t.db"
    args = '{"to": "604-721-8629", "body": "Booked for Tuesday."}'

    first = LiveWorld(now=NOW, rules=rules, store=SqliteStore(db), session_id="sess-1")
    first.open_ticket(phone="604-721-8629")
    registry.call(first, "sms_send", args, ("sms.send",))

    # A different process entirely: new world, new store handle, same session.
    second = LiveWorld(now=NOW, rules=rules, store=SqliteStore(db), session_id="sess-1")
    second.open_ticket(phone="604-721-8629")
    registry.call(second, "sms_send", args, ("sms.send",))

    assert len(sent.calls) == 1, "the customer's phone went twice"
    assert second.repeats, "the repeat was not recorded either"


def test_two_conversations_do_not_share_a_ledger(rules: dict, tmp_path: Path, live,
                                                 monkeypatch):
    """Two customers can be told the same sentence, and both must hear it."""
    live("sms.send")
    sent = _fake(monkeypatch, "twilio_sms", "send_sms",
                 Recorder({"provider": "twilio", "message_id": "SM1"}))
    registry.load_tools(None)
    db = tmp_path / "t.db"
    args = '{"to": "604-721-8629", "body": "Booked for Tuesday."}'

    for session in ("sess-1", "sess-2"):
        talk = LiveWorld(now=NOW, rules=rules, store=SqliteStore(db), session_id=session)
        talk.open_ticket(phone="604-721-8629")
        registry.call(talk, "sms_send", args, ("sms.send",))

    assert len(sent.calls) == 2


# ---- the follow-up that used to fire never ------------------------------


def test_a_follow_up_is_written_where_a_later_process_will_find_it(world: LiveWorld):
    """The conversation that arranges one ends. Held in its memory, it fires never — which
    is what the first generation shipped."""
    ticket = world.open_ticket(phone="604-721-8629")

    world.schedule_followup(ticket.id, 24)

    due = world.store.due_followups(world.now + timedelta(hours=25))
    assert [f["ticket_id"] for f in due] == [ticket.id]

"""The live world, against a real database and no network.

The switches are forced off for the whole module, so every outbound leg answers "not live"
and records what it would have sent. That is the state a laptop is in and the state these
tests need: what is being checked is that the *durable* half works — a ticket that is still
there after the process that made it, a customer the next conversation can find, an
appointment that blocks the slot, a follow-up somebody will actually be asked.

The simulator has none of that and cannot fail at it, which is why these are separate from
`test_engine.py`. Everything here would have passed vacuously against `flow/sim/world.py`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from flow.live.world import LiveWorld  # noqa: E402
from flow.runner.engine import Conversation  # noqa: E402
from flow.runner.graph import load  # noqa: E402
from flow.sim import tools as sim_tools  # noqa: E402
from flow.world import Refused  # noqa: E402
from plumbing.store import SqliteStore  # noqa: E402


@pytest.fixture(autouse=True)
def nothing_leaves_the_process(monkeypatch):
    """Belt and braces. `is_live` already reads the environment and these tests set it
    off, but a test that reached Twilio because somebody's shell had the switch on would
    cost real money and send a real stranger a real text."""
    monkeypatch.setenv("PLUMBING_LIVE_ENABLED", "false")
    monkeypatch.setenv("PLUMBING_LIVE_TOOLS", "")


@pytest.fixture()
def db(tmp_path) -> str:
    return str(tmp_path / "plumbing.db")


@pytest.fixture()
def world(db) -> LiveWorld:
    return LiveWorld(SqliteStore(db))


# ---- the model, scripted ----------------------------------------------


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeCall:
    id: str
    function: Any


@dataclass
class FakeMessage:
    content: str = ""
    tool_calls: list[FakeCall] = field(default_factory=list)


def says(text: str) -> FakeMessage:
    return FakeMessage(content=text)


def calls(*wanted: tuple[str, dict], text: str = "") -> FakeMessage:
    return FakeMessage(
        content=text,
        tool_calls=[
            FakeCall(id=f"c{i}",
                     function=FakeFunction(name.replace(".", "_", 1), json.dumps(args)))
            for i, (name, args) in enumerate(wanted)
        ],
    )


class ScriptedLLM:
    def __init__(self, *script: FakeMessage) -> None:
        self.script = list(script)

    def chat(self, role, messages, tools=None, **_: Any) -> FakeMessage:
        if not self.script:
            raise AssertionError("the conversation asked for more turns than the script has")
        return self.script.pop(0)


# ---- what outlives the process ----------------------------------------


def test_a_ticket_is_a_row_before_anybody_has_said_anything(world, db):
    """The engine opens one on construction. If that only existed in memory, a restart
    between the greeting and the booking would lose the whole record of the conversation
    and nobody would know a customer had ever been in touch."""
    ticket = world.open_ticket("604-555-0101")

    assert SqliteStore(db).ticket(ticket.id)["status"] == "New Inquiry"


def test_facts_written_onto_a_ticket_survive_the_process(world, db):
    ticket = world.open_ticket()
    world.remember(ticket.id, {"issue": "no hot water", "property_type": "house"})

    stored = SqliteStore(db).ticket(ticket.id)
    assert stored["tags"]["issue"] == "no hot water"
    assert stored["tags"]["property_type"] == "house"


def test_a_status_change_is_written_with_the_step_that_came_before_it(world, db):
    ticket = world.open_ticket()
    world.set_status(ticket.id, "Phone Verified")
    world.set_status(ticket.id, "Customer Identified")

    stored = SqliteStore(db).ticket(ticket.id)
    assert stored["status"] == "Customer Identified"
    assert stored["history"] == ["New Inquiry -> Phone Verified",
                                 "Phone Verified -> Customer Identified"]


def test_a_ticket_from_last_week_can_be_read_back_into_a_new_conversation(world, db):
    """Somebody texting about a job booked a fortnight ago arrives on a fresh world with
    an empty working set. Without the read-back the tool says there is no such ticket."""
    ticket = world.open_ticket("604-555-0101")
    world.remember(ticket.id, {"issue": "kitchen tap"})

    later = LiveWorld(SqliteStore(db))
    assert later.ticket(ticket.id).tags["issue"] == "kitchen tap"


def test_a_ticket_nobody_ever_opened_is_refused_by_name(world):
    with pytest.raises(Refused) as caught:
        world.ticket("TK-9999")
    assert "TK-9999" in str(caught.value)


# ---- who they are -----------------------------------------------------


def test_a_customer_created_now_is_found_by_the_next_conversation(world, db):
    world.add_customer(phone="604-555-0688", name="Wei", address="55 Fir Ave",
                       email="wei@example.com")

    found = LiveWorld(SqliteStore(db)).find_customer("+1 (604) 555-0688")
    assert found is not None
    assert (found.name, found.email) == ("Wei", "wei@example.com")


def test_a_returning_customer_arrives_with_their_open_work(world, db):
    """The summary a step reads is about *this* conversation, so without this somebody who
    rang last week is met as a stranger with a familiar name. The lookup is the only point
    in the whole graph where anything from before this conversation is visible at all."""
    world.add_customer(phone="604-555-0912", name="Ana Reyes", address="88 Alder St")
    ticket = world.open_ticket("604-555-0912")
    world.remember(ticket.id, {"issue": "no hot water upstairs"})
    world.set_status(ticket.id, "Escalated to Supervisor")

    # A week later, on a conversation that knows nothing.
    found = LiveWorld(SqliteStore(db)).find_customer("6045550912")

    assert len(found.open_work) == 1
    assert found.open_work[0]["about"] == "no hot water upstairs"
    assert found.open_work[0]["status"] == "Escalated to Supervisor"


def test_a_closed_job_is_not_open_work(world, db):
    """Otherwise every customer we have ever finished a job for arrives carrying it."""
    world.add_customer(phone="604-555-0912", name="Ana Reyes")
    ticket = world.open_ticket("604-555-0912")
    world.set_status(ticket.id, "Closed")

    assert LiveWorld(SqliteStore(db)).find_customer("604-555-0912").open_work == []


def test_open_work_is_a_conclusion_and_not_a_transcript(world, db):
    """A step gets what it can act on, not the words that reached it. Carrying the
    messages would be the whole conversation back in every prompt, which is the thing this
    design exists to avoid."""
    world.add_customer(phone="604-555-0912", name="Ana")
    ticket = world.open_ticket("604-555-0912")
    world.remember(ticket.id, {"issue": "no hot water"})
    world.store.add_message(channel="chat", speaker="customer", text="I am furious",
                            phone="604-555-0912", ticket_id=ticket.id)

    entry = LiveWorld(SqliteStore(db)).find_customer("604-555-0912").open_work[0]

    assert "furious" not in str(entry)
    assert set(entry) == {"ticket_id", "status", "about", "last_touched"}


def test_saving_a_customer_again_does_not_wipe_what_is_already_known(world):
    """A conversation that learns only a name must not lose the address taken last week —
    the customer would be asked for it again, which is the clearest sign nobody listened."""
    world.add_customer(phone="604-555-0688", name="Wei", address="55 Fir Ave", email="")
    world.add_customer(phone="604-555-0688", name="Wei Chen", address="", email="")

    found = world.find_customer("604-555-0688")
    assert (found.name, found.address) == ("Wei Chen", "55 Fir Ave")


# ---- the diary --------------------------------------------------------


def test_a_booking_blocks_the_slot_it_took(world):
    first = world.free_slots()[0]
    world.book(ticket_id=world.open_ticket().id, starts=first, minutes=120,
               technician="t_wang", address="55 Fir Ave", what="tap", phone="604-555-0688")

    assert first not in LiveWorld(world.store).free_slots()


def test_an_appointment_is_found_by_the_number_not_the_ticket(world, db):
    """A customer ringing about a visit booked last week is on a new ticket. Looking by
    ticket finds nothing, and the conversation tells them they have no appointment."""
    starts = world.free_slots()[0]
    world.book(ticket_id=world.open_ticket().id, starts=starts, minutes=120,
               technician="t_wang", address="55 Fir Ave", what="tap",
               phone="+1-604-555-0688")

    later = LiveWorld(SqliteStore(db))
    found = later.find_appointments("(604) 555 0688")
    assert len(found) == 1
    assert found[0].what == "tap"


def test_slots_are_offered_in_working_hours_only(world):
    opens = int(str(world.rules["schedule"]["working_hours"]["start"]).split(":")[0])
    closes = int(str(world.rules["schedule"]["working_hours"]["end"]).split(":")[0])

    for slot in world.free_slots(days=7, limit=8):
        assert opens <= slot.hour < closes
        assert slot.weekday() != 6           # Sunday; the business is shut


# ---- work owed to a person -------------------------------------------


def test_a_follow_up_is_a_row_the_reminder_loop_will_find(world, db):
    """The conversation that arranged it is over. Held in memory it fires exactly never,
    which is what happened before the table existed."""
    ticket = world.open_ticket("604-555-0688")
    world.remember(ticket.id, {"issue": "no hot water"})
    world.schedule_followup(ticket.id, hours=24)

    store = SqliteStore(db)
    assert store.due_followups(world.now + timedelta(hours=23)) == []
    due = store.due_followups(world.now + timedelta(hours=25))
    assert len(due) == 1
    assert due[0]["chat_id"] == world.on_duty().telegram
    assert "no hot water" in due[0]["summary"]


def test_an_escalation_reaches_a_person_and_not_just_a_table(world):
    """No node in the graph holds both `escalate.raise` and `technician.notify`. Recorded
    and left there, an escalation is a row nobody is watching — and the customer has been
    told a technician will come back to them."""
    ticket = world.open_ticket("604-555-0688")
    world.escalate(ticket.id, "a project to price", "whole upstairs bathroom")

    assert len(world.escalations) == 1
    assert len(world.technician_messages) == 1
    assert world.technician_messages[0]["technician_id"] == world.on_duty().id


def test_only_technicians_who_can_be_reached_are_offered(world):
    """Telegram is the only way the office reaches a technician. Somebody with no chat id
    cannot be told, so putting them in front of the model buys a booking that fails after
    the diary entry exists."""
    assert world.technicians
    assert all(person.telegram for person in world.technicians.values())


def test_notifying_somebody_who_is_not_on_the_roster_is_refused(world):
    with pytest.raises(Refused) as caught:
        world.notify_technician("t_nobody", "a job", "details")
    assert "t_nobody" in str(caught.value)


# ---- the whole thing, driven by the engine ----------------------------


def test_a_scripted_booking_leaves_every_record_behind(db):
    """The mechanics end to end: the engine over the live world over the store.

    Scripted rather than real, because what is being checked is the plumbing — that each
    tool's effect landed in the database — and a real model would make it hard to see
    which of them had.
    """
    store = SqliteStore(db)
    world = LiveWorld(store)
    flow = load(known_tools=sim_tools.names())
    slot = world.free_slots()[0]

    talk = Conversation(
        world,
        ScriptedLLM(
            calls(("calendar.create_appointment",
                   {"ticket_id": "TK-0001", "starts": slot.isoformat(),
                    "address": "55 Fir Ave", "what": "dripping tap"})),
            calls(("technician.notify",
                   {"technician_id": "t_wang", "subject": "New job",
                    "body": "55 Fir Ave, Wei, 604-555-0688, dripping tap"}),
                  ("sms.send", {"to": "604-555-0688", "body": "You're booked in."}),
                  ("schedule.create_followup", {"ticket_id": "TK-0001", "hours": 24})),
            says("You're booked in for that slot. No need to wait here."),
        ),
        flow,
        start_at="booking",
        known={"phone": "604-555-0688", "customer_name": "Wei"},
    )

    reply = talk.say("yes please, that time works")

    assert talk.finished
    assert "booked in" in reply.reply
    assert store.ticket(talk.ticket_id)["status"] == "Appointment Booked"
    assert len(store.appointments_between(world.now, world.now + timedelta(days=8))) == 1
    assert len(store.due_followups(world.now + timedelta(hours=25))) == 1
    assert world.texts and world.technician_messages


def test_the_conversation_is_written_down_as_it_happens(db):
    """A node's messages are dropped when the flow moves on — that is where the context
    saving comes from — so this is the only place the exchange exists afterwards."""
    from plumbing.live.flow_conversation import FlowConversation

    talk = FlowConversation(
        store=SqliteStore(db),
        llm=ScriptedLLM(says("Hello — what's gone wrong?")),
        channel="chat", phone="604-555-0688", session_id="s1",
    )
    talk.say("my tap is dripping")

    said = SqliteStore(db).conversation(session_id="s1")
    assert [m["speaker"] for m in said] == ["customer", "agent"]
    assert said[0]["text"] == "my tap is dripping"
    assert said[1]["ticket_id"] == talk.ticket_id


def test_the_number_the_channel_supplied_is_on_the_ticket_before_the_first_word(db):
    """Every live channel has one. Asking somebody for the number they are speaking to us
    on is the clearest possible sign nobody is listening, so `identify` is handed it."""
    from plumbing.live.flow_conversation import FlowConversation

    talk = FlowConversation(
        store=SqliteStore(db), llm=ScriptedLLM(), channel="sms", phone="604-555-0688",
    )

    assert talk.talk.tags["phone"] == "604-555-0688"
    assert SqliteStore(db).ticket(talk.ticket_id)["phone"] == "604-555-0688"


def test_a_silent_turn_mid_conversation_does_not_read_as_a_goodbye(db):
    """Two steps can each finish without speaking, and the turn ends with nothing to send.

    The filler that fills it must never say more than the truth. The first version said
    "that's all in hand at our end" and went out in the middle of a booking — the customer
    was told they were finished while the conversation carried on around them, which is
    the same lie the terminal-step guard exists to prevent.
    """
    from plumbing.live import flow_conversation as fc

    # Every node silent until the fuse blows. Reaching this at all now means something has
    # run away — the ordinary silent stretches are shorter than the ceiling, which is the
    # whole point of `MAX_NODES_PER_TURN` being a fuse rather than a budget.
    from flow.runner.engine import MAX_NODES_PER_TURN

    silent = [calls(("step.finished", {"outcome": "done"}))] * (MAX_NODES_PER_TURN - 1)
    talk = fc.FlowConversation(
        store=SqliteStore(db),
        llm=ScriptedLLM(
            calls(("ticket.set_fields", {"ticket_id": "TK-0001", "fields": {"issue": "a leak"}}),
                  ("step.finished", {"outcome": "new"})),         # identify -> new_customer
            *silent,
        ),
        channel="chat", phone="604-555-0688",
    )

    assert talk.say("hi") == fc.STILL_GOING
    assert not talk.closed
    assert talk.node != "identify"                 # it really did move

    # And it is written down like any other reply. The engine reports only what it
    # produced, so an unrecorded filler leaves a transcript where the customer answers a
    # message nobody sent.
    said = SqliteStore(db).conversation(phone="604-555-0688")
    assert [m["text"] for m in said] == ["hi", fc.STILL_GOING]


def test_a_silent_ending_says_goodbye_rather_than_nothing(db):
    """Everything worked, nobody spoke. An empty reply reaches the widget as a failure and
    the customer is told we could not be reached, on the turn we did the whole job."""
    from plumbing.live import flow_conversation as fc

    talk = fc.FlowConversation(
        store=SqliteStore(db), llm=ScriptedLLM(), channel="chat", phone="604-555-0688",
    )
    talk.talk.finished = True
    talk.talk.say = lambda text: type("T", (), {"reply": ""})()

    assert talk.say("thanks") == fc.NOTHING_MORE_TO_SAY


def test_coming_back_later_opens_a_new_ticket_that_still_knows_the_number(db):
    """A booked job and a new leak a week later are two pieces of work. The second gets
    its own ticket — and would get one knowing nothing, so the customer would be asked for
    the number they are texting us from."""
    from plumbing.live.flow_conversation import FlowConversation

    talk = FlowConversation(
        store=SqliteStore(db),
        llm=ScriptedLLM(says("All sorted."), says("Oh no — what's happened?")),
        channel="sms", phone="604-555-0688",
    )
    first = talk.ticket_id
    talk.talk.finished = True

    talk.say("actually the shower is leaking now")

    assert talk.ticket_id != first
    assert talk.talk.tags["phone"] == "604-555-0688"

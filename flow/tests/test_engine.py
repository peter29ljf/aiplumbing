"""Walking the graph, with a scripted model instead of a real one.

No API calls. These are about the mechanics — when a node ends, what survives it, what
happens when the model answers with a branch nobody named — and those are exactly the
places where a real model would make the behaviour hard to see.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from flow.runner.engine import Conversation  # noqa: E402
from flow.runner.graph import load  # noqa: E402
from flow.sim import tools as sim_tools  # noqa: E402
from flow.sim.world import World  # noqa: E402

NOW = "2026-08-05T10:00:00-07:00"


# ---- a model that says exactly what the test wants -------------------


@dataclass
class FakeCall:
    id: str
    function: Any


@dataclass
class FakeFunction:
    name: str
    arguments: str


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
            FakeCall(id=f"c{i}", function=FakeFunction(name.replace(".", "_", 1),
                                                       json.dumps(args)))
            for i, (name, args) in enumerate(wanted)
        ],
    )


class ScriptedLLM:
    """Hands back prepared messages and records the prompt each one was asked with."""

    def __init__(self, *script: FakeMessage) -> None:
        self.script = list(script)
        self.prompts: list[str] = []
        self.tool_sets: list[list[str]] = []

    def chat(self, role, messages, tools=None, **_: Any) -> FakeMessage:
        self.prompts.append(messages[0]["content"])
        self.tool_sets.append(
            [t["function"]["name"] for t in (tools or [])]
        )
        if not self.script:
            raise AssertionError("the conversation asked for more turns than the script has")
        return self.script.pop(0)


@pytest.fixture()
def flow():
    return load(known_tools=sim_tools.names())


def _talk(llm, flow, **seed) -> Conversation:
    return Conversation(World(now=NOW, seed=seed or None), llm, flow)


# ---- what a node is given ---------------------------------------------


def test_a_node_is_given_only_its_own_tools(flow):
    llm = ScriptedLLM(says("Hello — what has gone wrong?"))
    _talk(llm, flow).say("hi")

    assert llm.tool_sets[0] == ["ticket_set_fields", "step_finished"]


def test_a_node_prompt_does_not_mention_the_rest_of_the_graph(flow):
    llm = ScriptedLLM(says("Hello."))
    _talk(llm, flow).say("hi")

    prompt = llm.prompts[0]
    for other in ("scheduling", "large_project", "warranty_check", "booking"):
        assert other not in prompt


def test_every_node_prompt_stays_small(flow):
    """The whole reason for the rewrite. The old agents send 42,968 characters a call.

    This measured the first node only, against 4,000 — a figure taken when `greeting` was
    the one node with no rules file at all. It has one now, and that file is why three
    scenarios stopped failing, so the number moved. It moved to a ceiling over every node
    rather than a reading of one, which is the thing actually worth holding: 8,000 is
    under a fifth of what a single old call cost, and the fattest node here is 6,600.
    """
    from flow.runner.assemble import build

    biggest = max((len(build(node)), node.name) for node in flow.nodes.values())

    assert biggest[0] < 8_000, biggest


# ---- moving on --------------------------------------------------------


def test_a_node_ends_when_the_outcome_is_set(flow):
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"}), text="Right."),
        says("May I have your phone number?"),
    )
    conversation = _talk(llm, flow)

    conversation.say("hi")

    assert conversation.node.name == "identify"


def test_the_previous_node_s_conversation_is_dropped(flow):
    """Where the context saving actually comes from. Without this the fourth node would
    be carrying the first three nodes' exchanges and the small prompts stop being small."""
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"})),
        says("May I have your phone number?"),
    )
    conversation = _talk(llm, flow)

    conversation.say("my sink is leaking everywhere and I am very upset about it")

    assert "very upset" not in json.dumps(conversation.messages)


def test_what_survives_is_the_summary_not_the_words(flow):
    llm = ScriptedLLM(
        calls(("ticket.set_fields", {"ticket_id": "TK-0001", "fields": {"customer_name": "Lin"}}),
              ("step.finished", {"outcome": "done"})),
        says("Thanks Lin."),
    )
    conversation = _talk(llm, flow)

    conversation.say("hello")

    assert "Lin" in llm.prompts[-1]          # the next node was told who this is


def test_a_branch_is_taken_by_name(flow):
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"})),           # greeting
        calls(("step.finished", {"outcome": "existing"})),       # identify
        says("Welcome back."),
    )
    conversation = _talk(llm, flow)

    conversation.say("hi")

    assert conversation.node.name == "warranty_check"


def test_an_outcome_nobody_named_is_handed_back(flow):
    """Rather than picking a branch on the model's behalf, which is a decision made by
    accident and impossible to find afterwards."""
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"})),
        calls(("step.finished", {"outcome": "maybe"})),
        says("Sorry — have we worked for you before?"),
    )
    conversation = _talk(llm, flow)

    turn = conversation.say("hi")

    assert conversation.node.name == "identify"             # did not move
    assert "maybe" in json.dumps(conversation.messages)
    assert turn.reply == "Sorry — have we worked for you before?"


# ---- the ticket -------------------------------------------------------


def test_the_status_follows_the_node(flow):
    """Nothing calls a status tool. The ticket goes where the node says it goes, which is
    one fewer thing for the model to get wrong."""
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"})),
        says("Number please?"),
    )
    conversation = _talk(llm, flow)

    conversation.say("hi")

    assert conversation.world.tickets["TK-0001"].status == "New Inquiry"
    assert conversation.world.tickets["TK-0001"].history == []


def test_the_ways_out_are_an_enum_the_model_cannot_step_outside(flow):
    """Naming a branch that does not exist stops being possible rather than being asked
    for. The identify node has exactly four ways out and no fifth."""
    from flow.sim import tools as st

    node = flow["identify"]
    schemas = st.schemas_for(node.tools, outcomes=node.choices)
    finished = [s for s in schemas if s["function"]["name"] == "step_finished"][0]

    assert finished["function"]["parameters"]["properties"]["outcome"]["enum"] == [
        "new", "existing", "booking_change", "no_number"]


def test_the_outcome_does_not_stay_on_the_ticket(flow):
    """It is a signal to the engine, not a fact about the customer. Left behind, the next
    node reads it as a decision that has already been made."""
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"})),
        says("Number please?"),
    )
    conversation = _talk(llm, flow)

    conversation.say("hi")

    assert "outcome" not in conversation.world.tickets["TK-0001"].tags


# ---- ending -----------------------------------------------------------


def test_a_reply_comes_back_with_what_it_cost(flow):
    llm = ScriptedLLM(says("Hello there."))

    turn = _talk(llm, flow).say("hi")

    assert turn.reply == "Hello there."
    assert turn.nodes == ["greeting"]
    assert len(turn.steps) == 1


def test_a_tool_the_node_does_not_have_is_refused_by_name(flow):
    """A node cannot reach past its own list, and is told so rather than left guessing."""
    llm = ScriptedLLM(
        calls(("calendar.find_slots", {})),
        says("Let me get a few details first."),
    )
    conversation = _talk(llm, flow)

    conversation.say("when can you come?")

    tool_reply = [m for m in conversation.messages if m.get("role") == "tool"][0]
    assert "not available here" in tool_reply["content"]


# ---- what outlives a step ---------------------------------------------


def test_a_fact_a_tool_handled_is_kept_without_being_asked(flow):
    """A customer gave their number, the lookup used it, the step ended, and the number
    went with the messages — so the next step asked for it again. Being asked twice for the
    same thing is the clearest sign nobody is listening, and it should not depend on the
    model remembering to write things down."""
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"}), text="Noted — one moment."),
        calls(("crm.lookup_by_phone", {"phone": "604 555 0166"})),
        says("You're not on file yet."),
    )
    conversation = _talk(llm, flow)

    conversation.say("hi")
    conversation.say("604 555 0166")

    assert conversation.tags["phone"] == "604 555 0166"


def test_what_a_lookup_found_is_kept_too(flow):
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"}), text="Noted — one moment."),
        calls(("crm.lookup_by_phone", {"phone": "604-555-7788"})),
        says("Welcome back, Emily."),
    )
    conversation = _talk(llm, flow, customers=[
        {"phone": "604-555-7788", "name": "Emily Carter", "address": "4321 Hastings St"}
    ])

    conversation.say("hi")
    conversation.say("604-555-7788")

    assert conversation.tags["name"] == "Emily Carter"
    assert conversation.tags["address"] == "4321 Hastings St"


def test_a_tool_that_handles_nothing_lasting_keeps_nothing(flow):
    """Not everything a tool touches is a fact about the customer. Hoovering up whatever
    goes past would put the contents of a rules lookup on the ticket."""
    from flow.sim import tools as st

    assert st._TOOLS["clock.now"]["remembers"] == ()
    assert st._TOOLS["rules.get_job_sizing"]["remembers"] == ()


def test_a_terminal_node_ends_the_conversation_by_replying(flow):
    """It had done the booking, the text, the technician and the follow-up, said the
    confirmation — and left the conversation open because one tool call was missing. The
    graph already knows which nodes are the end; the model does not need to say so."""
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"})),                      # greeting
        calls(("step.finished", {"outcome": "existing"})),                  # identify
        calls(("step.finished", {"outcome": "claim"})),                     # warranty_check
        calls(("escalate.raise", {"ticket_id": "TK-0001", "reason": "warranty",
                                  "details": "the tap they fixed drips again"}),
              ("schedule.create_followup", {"ticket_id": "TK-0001", "hours": 24}),
              text="That's with the technician now — he'll come back to you."),
    )
    conversation = _talk(llm, flow)

    turn = conversation.say("my repair has failed again")

    assert conversation.finished
    assert turn.reply.startswith("That's with the technician")
    assert conversation.world.escalations


def test_a_last_step_cannot_sign_off_with_its_work_undone(flow):
    """`booking` told a customer "you're all set" having created no appointment, sent no
    text and told no technician — and because a reply ends the conversation, that was the
    end of it. The tools a last step holds are its job."""
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"})),
        calls(("step.finished", {"outcome": "existing"})),
        calls(("step.finished", {"outcome": "claim"})),
        says("All done, somebody will be in touch."),        # nothing actually done
        calls(("escalate.raise", {"ticket_id": "TK-0001", "reason": "warranty",
                                  "details": "tap drips again"}),
              ("schedule.create_followup", {"ticket_id": "TK-0001", "hours": 24}),
              text="Passed to the technician — he'll come back to you."),
    )
    conversation = _talk(llm, flow)

    turn = conversation.say("my repair has failed again")

    assert conversation.world.escalations                  # it was made to do the work
    assert turn.reply.startswith("Passed to the technician")
    assert conversation.finished


def test_bookkeeping_is_not_work(flow):
    """Writing a field down is not doing the thing, so set_fields alone does not count as
    a last step having finished."""
    from flow.runner.engine import Conversation

    node = flow["warranty_handover"]
    turn = type("T", (), {"steps": []})()

    assert "escalate.raise" in Conversation._undone(
        type("C", (), {"NOT_WORK": Conversation.NOT_WORK})(), node, turn)


def test_a_last_step_is_told_to_let_them_go(flow):
    """One scenario ended on "could you email a few photos?" and the conversation was over
    — they answer into nothing and nobody reads it. Another stated a refusal and simply
    stopped, leaving the customer watching a chat that had already ended."""
    from flow.runner.assemble import build

    for node in flow.nodes.values():
        if node.is_terminal:
            prompt = build(node)
            assert "no need to stay online" in prompt.lower(), node.name
            assert "Never finish on a question" in prompt, node.name


def test_coming_back_after_it_ended_starts_a_new_one(flow):
    """A booked job and a new leak a week later are two pieces of work. Continuing into
    the closed one would put the second on a ticket already settled, which is a record
    nobody looks at again."""
    llm = ScriptedLLM(
        calls(("step.finished", {"outcome": "done"})),
        calls(("step.finished", {"outcome": "existing"})),
        calls(("step.finished", {"outcome": "claim"})),
        calls(("escalate.raise", {"ticket_id": "TK-0001", "reason": "warranty",
                                  "details": "tap drips"}),
              ("schedule.create_followup", {"ticket_id": "TK-0001", "hours": 24}),
              text="With the technician now — no need to stay online."),
        says("Hello again — what has happened?"),
    )
    conversation = _talk(llm, flow)
    conversation.say("my repair failed")
    first = conversation.ticket_id

    turn = conversation.say("actually the boiler has gone now too")

    assert not conversation.finished
    assert conversation.node.name == flow.entry
    assert conversation.ticket_id != first        # a second job, a second ticket
    assert turn.reply == "Hello again — what has happened?"

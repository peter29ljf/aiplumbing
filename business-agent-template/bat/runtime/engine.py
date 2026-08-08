"""One conversation walking the graph.

**A node's messages do not survive the node.** When the flow moves on, the exchange that
got it there is dropped and the next node is handed the summary instead — who this is,
what is wrong, what has been decided. That is where the context saving actually happens:
without it, every node would carry the whole conversation and the small prompts would stop
being small by the fourth turn.

A node ends when the model calls `step.finished`. It was a field on `ticket.set_fields`
first, and for seven exchanges a real model wrote every other field faithfully and never
that one — the tool it lived on is described as the place to record what you have learned
about a customer, and routing a conversation is not that. One tool records facts, one says
a step is over.

Nothing here infers that a step is finished from what was said. Inferring it is how a flow
ends up somewhere nobody can explain.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from bat.runtime import assemble, memory
from bat.runtime import registry as sim_tools
from bat.runtime.graph import Flow, Node
from bat.runtime.world import AnyWorld

# One customer message must not be able to spend the afternoon. Generous enough that a
# node asking two questions and calling three tools is nowhere near it.
MAX_CALLS_PER_TURN = 8
# A node that finishes without saying anything hands straight on to the next. More than a
# few of those in one turn means the graph is walking itself, which is worth stopping.
#
MAX_NODES_PER_TURN = 4

# The parts of a world snapshot a claim can be checked against. Counted before and after
# each model call, so "it said it booked something" can be answered with "did an
# appointment appear", which is a question about the world rather than about wording.
COUNTED = ("appointments", "texts", "emails", "technician_messages", "escalations",
           "followups")


def _tally(world: Any) -> dict[str, int]:
    snapshot = world.snapshot()
    counts = {key: len(snapshot.get(key) or ()) for key in COUNTED}
    # Status moves too: a ticket walking to a new state is a change somebody may claim.
    counts["status_moves"] = sum(len(t.get("history") or ())
                                 for t in (snapshot.get("tickets") or {}).values())
    return counts


def _changed(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after[key] - before[key] for key in after if after[key] > before.get(key, 0)}


NOTHING_SAID = (
    "[system] You did not say anything to the customer. Reply in plain text now."
)


@dataclass
class Step:
    """What one model call was given, what it did with it, and what that cost.

    `offered` matters as much as `tools`. Telling a failure caused by a missing tool from
    one caused by a tool that was there and not used is the whole difference between
    fixing the configuration and changing the model, and it cannot be told afterwards
    from the transcript.
    """

    node: str
    seconds: float
    tools: list[str] = field(default_factory=list)
    offered: list[str] = field(default_factory=list)
    said: bool = False
    refusals: list[str] = field(default_factory=list)
    # What this step actually said, and what changed in the world while it said it.
    #
    # `text` because the alternative was zipping the speaking steps against the
    # transcript's agent lines and trusting them to stay in step. They do until one turn
    # joins a step's parting words to the next step's answer — one line for two speaking
    # steps — after which every pairing is silently wrong and the wrong node gets blamed.
    #
    # `delta` because "did it claim something that did not happen" is a question about the
    # world, not about wording. Judged from words, a step saying "I won't say you're
    # booked just yet" was reported as claiming a booking.
    text: str = ""
    # What the tools answered this step, flattened. The grounding a figure is checked
    # against — without it, "your reference is TB-0007" cannot be told from a lookup.
    saw: str = ""
    delta: dict[str, int] = field(default_factory=dict)


@dataclass
class Turn:
    reply: str
    steps: list[Step] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    # What a step said on its way out, held until the step after it speaks. The two are
    # sent together: "Got it — a dripping tap. May I have your phone number?" rather than
    # the first half now and the second half only if the customer asks what is happening.
    carried: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return round(sum(step.seconds for step in self.steps), 1)


# How many replies a step gets before it is asked what it is still waiting for, and what
# it is asked. One copy, because there were three and the shortest of them dropped the
# reason — and a rule with its reason attached is followed far more reliably than the same
# rule bare, which is the single most repeated finding in this project.
#
# Two, not three. `warranty_check` looked the old job up on its first call, wrote it down
# on its second, and then argued with an angry customer for four turns — the nudge landed
# on the fourth and it finished on the fifth, by which time the customer had asked the
# same question four ways.
REPLIES_BEFORE_A_NUDGE = 2


def nudge(node: Any, spoke: int) -> str:
    """A step that keeps talking and never finishes, told so.

    The mirror of the closing gate. That one stops a last step signing off before its work
    is done; this stops every other step doing the opposite — talking indefinitely with
    the way out in front of it. `greeting` spent seventeen model calls sympathising with
    an angry customer, and `offer_options`, cornered by "so is it booked?", answered yes.
    Both had `step.finished` the whole time.

    A step is not stuck because it lacks patience. It is stuck because what the customer
    is pressing for lives further down the graph, and the only way to reach it is to
    finish.
    """
    if getattr(node, "is_terminal", False) or spoke < REPLIES_BEFORE_A_NUDGE:
        return ""
    return (
        f"[system] You have replied {spoke} times from this step without calling "
        f"step.finished. If this step's goal is met, call it now. If you are waiting "
        f"on something this step has no tool for, a later step has it — finishing is "
        f"how they get it, and repeating yourself is not."
    )


class Conversation:
    def __init__(self, world: AnyWorld, llm: Any, flow: Flow, *,
                 start_at: str = "", known: dict[str, Any] | None = None) -> None:
        """`start_at` begins part-way down the graph, with `known` already on the ticket.

        Reaching `booking` from the top costs eight nodes and twenty model calls, and
        nineteen of them are testing nodes that have not failed in days. Starting at the
        node under test is only sound because of how this is built: a node reads the
        ticket, never the transcript, so a ticket carrying the right facts is
        indistinguishable from having walked there. If that ever stops being true, these
        stop being valid — which is itself worth knowing.
        """
        self.world = world
        self.llm = llm
        self.flow = flow
        self.node: Node = self.flow[start_at or self.flow.entry]
        # Opened here, not by a tool. It was a tool, and twice out of six the model
        # finished the first step without calling it — after which every set_fields had
        # nowhere to write and the whole conversation kept no record at all. Opening a
        # ticket is bookkeeping, not a decision, and nothing is served by asking.
        self.ticket_id = world.open_ticket().id
        self.entry = self.node.name
        if known:
            self.world.tickets[self.ticket_id].tags.update(known)
            if known.get("phone"):
                self.world.tickets[self.ticket_id].phone = str(known["phone"])
        self.messages: list[dict[str, Any]] = []
        self.finished = False

    # ------------------------------------------------------------------
    def save(self) -> dict[str, Any]:
        """Everything needed to pick this conversation up somewhere else.

        Three things, and the shortness of the list is the architecture's doing rather
        than luck: a node reads the ticket and never the transcript, and the message list
        is thrown away at every boundary. So what survives a crash is the world, which
        node we are standing in, and which ticket we are writing to. At worst the exchange
        in progress is lost, and the customer repeats one sentence instead of everything.

        The in-flight messages are deliberately not saved. They belong to the node that is
        running, they are discarded the moment it finishes anyway, and writing them down
        would make the record larger than the thing it protects.
        """
        return {"world": self.world.save(), "node": self.node.name,
                "entry": self.entry, "ticket_id": self.ticket_id,
                "finished": self.finished}

    @classmethod
    def resume(cls, state: dict[str, Any], llm: Any, flow: Flow, *,
               rules: dict[str, Any]) -> "Conversation":
        """Carry on from `save()`, on a world rebuilt from the same record."""
        from bat.runtime.sim import World

        world = World.restore(state["world"], rules=rules)
        talk = cls(world, llm, flow, start_at=state["node"])
        # `__init__` opens a ticket, because a new conversation needs one. This is not a
        # new conversation: the ticket it was writing to is in the record, and the one
        # just opened is an empty duplicate nobody would ever read.
        world.tickets.pop(talk.ticket_id, None)
        talk.ticket_id = state["ticket_id"]
        talk.entry = state.get("entry") or talk.entry
        talk.finished = bool(state.get("finished"))
        return talk

    # ------------------------------------------------------------------
    @property
    def tags(self) -> dict[str, Any]:
        ticket = self.world.tickets.get(self.ticket_id)
        return ticket.tags if ticket else {}

    def say(self, text: str) -> Turn:
        """Feed one customer message and get back what to send them."""
        if self.finished:
            # They came back. A booked job and a new leak a week later are two pieces of
            # work, and continuing into a closed one would put the second on a ticket
            # already settled — a record nobody looks at again. Starting over costs
            # nothing: the lookup finds them in a moment and they are asked nothing twice.
            self._start_again()

        turn = Turn(reply="")
        self.messages.append({"role": "user", "content": text})

        # What the customer will see, joined. A step that acknowledges and then hands on
        # used to end the turn on its acknowledgement: "Got it — a dripping tap." and
        # nothing else, while the step that would have asked for their phone number waited
        # for them to speak again. They had nothing to answer, so they sat there. A real
        # customer testing this by hand typed "?" six times in one conversation, once for
        # each of these.
        #
        # The prompt asks a step with nothing to add to say nothing at all, and it mostly
        # does not — an acknowledgement is a very natural thing to write. So the engine
        # carries it: what the acknowledging step said and what the next step asks arrive
        # together, as one message, which is what a person would have sent anyway.
        said: list[str] = []

        for _ in range(MAX_NODES_PER_TURN):
            reply = self._run_node(turn)
            if reply is not None:
                said.append(reply)
                break
            if self.finished:
                break
        turn.reply = " ".join(part for part in [*turn.carried, *said] if part).strip()
        return turn

    # ------------------------------------------------------------------
    def _run_node(self, turn: Turn) -> str | None:
        """Work the current node. Returns what to say, or None if it moved on silently."""
        node = self.node
        turn.nodes.append(node.name)
        messages = [{"role": "system", "content": self._system()}, *self.messages]
        if (nudge := self._still_here(node)):
            messages.append({"role": "user", "content": nudge})

        for _ in range(MAX_CALLS_PER_TURN):
            began = time.monotonic()
            before = _tally(self.world)
            offered = sim_tools.schemas_for(node.tools,
                                             outcomes=node.choices or ("done",))
            message = self.llm.chat("agent", messages, tools=offered or None)
            calls = list(getattr(message, "tool_calls", None) or [])
            step = Step(
                node=node.name,
                seconds=round(time.monotonic() - began, 1),
                tools=[c.function.name for c in calls],
                offered=[t["function"]["name"] for t in (offered or [])],
                said=bool((message.content or "").strip()),
                text=(message.content or "").strip(),
            )
            turn.steps.append(step)

            messages.append(_assistant(message, calls))
            said = (message.content or "").strip()

            if not calls:
                if node.is_terminal and said:
                    if self._ends_here(node, turn, messages):
                        return said
                    # Told what it still has to do. Go round again rather than sending a
                    # sign-off the customer would believe.
                    continue
                self.messages = messages[1:]        # keep the exchange, drop the system
                return said or None

            outcome: str | None = None
            for call in calls:
                result, keep = sim_tools.call(
                    self.world, call.function.name, call.function.arguments, node.tools
                )
                self._absorb(result, keep)
                if isinstance(result, dict):
                    if result.get("finished"):
                        outcome = str(result.get("outcome", ""))
                    if result.get("ok") is False:
                        step.refusals.append(f"{call.function.name}: {result['error']}")
                answered = json.dumps(result, default=str)
                step.saw += " " + answered
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": answered})

            step.delta = _changed(before, _tally(self.world))

            if outcome is not None:
                spoke = (message.content or "").strip()
                self._advance(str(outcome))
                if spoke:
                    # Held, not sent. `say` joins it to whatever the next step asks.
                    turn.carried.append(spoke)
                return None

            # A last step may do its work and say its piece in one message. Checked here
            # as well as above, because a reply that arrives alongside tool calls used to
            # take the other path and the conversation stayed open.
            if node.is_terminal and said:
                if self._ends_here(node, turn, messages):
                    return said
                continue

        self.messages = messages[1:]
        return None

    # ------------------------------------------------------------------
    def _start_again(self) -> None:
        """A fresh conversation, on a fresh ticket, from where this one began."""
        self.finished = False
        self.node = self.flow[self.entry]
        self.messages = []
        self.ticket_id = self.world.open_ticket().id


    def _still_here(self, node: Node) -> str:
        spoke = sum(1 for m in self.messages
                    if m.get("role") == "assistant" and (m.get("content") or "").strip())
        return nudge(node, spoke)

    # Bookkeeping, not work: writing a field down is not doing the thing.
    # What a last step must have done before it can sign off: the things that reach
    # outside this process. Not everything it holds.
    #
    # It used to be "every tool except these two", and that made a tool compulsory the
    # moment it was granted. A step answering general questions was given the delivery
    # checker so it could answer one about delivery — and a customer who asked about
    # opening hours, having no address, left it unable to finish. It went round three
    # times and the scenario failed for circling.
    #
    # A lookup is never somebody's job. Nobody's work is to have looked something up, and
    # a step that answers a question without needing the diary has done its work. An
    # action is always the job: a booking step that signs off having booked nothing is the
    # exact failure this gate was built for. `once=True` already names that distinction —
    # it marks the tools that touch a diary, a phone, or a person — so the gate reads it
    # rather than keeping a second list that has to agree with the first.
    NOT_WORK = frozenset({"ticket.set_fields", "step.finished"})

    def _ends_here(self, node: Node, turn: Turn, messages: list[dict[str, Any]]) -> bool:
        """A last step's reply ends the conversation — once its work is actually done.

        Ending used to need a tool call the model kept forgetting, so everything got done
        and the conversation stayed open. Making the reply end it opened the opposite
        hole: `booking` told a customer "you're all set" having created no appointment,
        sent no text and told no technician, and that was the end of it.

        The tools a last step holds are its job. Nothing here reads the wording.
        """
        outstanding = self._undone(node, turn)
        if outstanding:
            messages.append({
                "role": "user",
                "content": f"[system] Not yet — you have not called: "
                           f"{', '.join(outstanding)}. Do that now, then say your closing "
                           f"message.",
            })
            return False

        self.world.set_status(self.ticket_id, node.sets_status)
        self.finished = True
        return True

    def _undone(self, node: Node, turn: Turn) -> list[str]:
        """The node's own tools it has not used yet, in this node, this conversation."""
        used = {tool for step in turn.steps if step.node == node.name for tool in step.tools}
        return [
            name for name in node.tools
            if sim_tools.reaches_outside(name) and name.replace(".", "_", 1) not in used
        ]

    def _system(self) -> str:
        return assemble.build(self.node, tags=self.tags, ticket_id=self.ticket_id)

    def _absorb(self, result: Any, keep: dict[str, Any]) -> None:
        """Take what the engine needs, and put what outlives this step on the ticket.

        `keep` comes from the tool's own `remembers` list, not from the model deciding to
        write it down. It was the model's job once, and a customer was asked for their
        phone number twice in one conversation because a step ended before anybody had
        recorded it.
        """
        if isinstance(result, dict) and result.get("ended"):
            self.finished = True

        if keep and self.ticket_id:
            self.world.tickets[self.ticket_id].tags.update(keep)

    def _advance(self, outcome: str) -> None:
        """Move on, and forget how we got here.

        The message list goes. What the next node knows is the summary on the ticket —
        which is exactly as much as it should need, and if it turns out not to be, the
        answer is to write more onto the ticket rather than to carry the transcript.
        """
        self.world.set_status(self.ticket_id, self.node.sets_status)

        if self.node.branch:
            target = self.node.branch.get(outcome)
            if target is None:
                # An outcome nobody named. Say so and let it choose again rather than
                # picking a branch on its behalf, which is a decision made by accident.
                self.messages.append({
                    "role": "user",
                    "content": f"[system] '{outcome}' is not one of the ways out of this "
                               f"step. Choose one of: {list(self.node.branch)}.",
                })
                return
        else:
            target = self.node.next

        if target is None:
            self.finished = True
            return

        self.node = self.flow[target]
        memory.remember_node(self.tags, self.node.name)
        self.messages = []


def _assistant(message: Any, calls: list[Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    if calls:
        payload["tool_calls"] = [
            {"id": c.id, "type": "function",
             "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls
        ]
    return payload

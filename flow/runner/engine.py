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
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from flow.runner import assemble, memory
from flow.runner.graph import Flow, Node, load
from flow.sim import tools as sim_tools
from flow.sim.world import World

# One customer message must not be able to spend the afternoon. Generous enough that a
# node asking two questions and calling three tools is nowhere near it.
MAX_CALLS_PER_TURN = 8

# A fuse, not a budget.
#
# A node that finishes without saying anything hands straight on to the next, and a turn
# owes the customer one reply — so the only reasons to stop without one are that the
# conversation ended or that something has genuinely run away. "It walked four nodes" is
# not one of them: the longest ordinary silent stretch in this graph is six hops,
# `identify → new_customer → property_ask → property_route → problem → sizing →
# offer_options`, and every one of those is a step doing its job.
#
# It was four, and it cut that stretch in half. A customer said "house", four nodes moved
# in silence, the turn ended with nothing to send, and the fallback line went out — "bear
# with me one moment", from a conversation that had just stopped. `offer_options` was next
# and would have spoken immediately. They waited, then typed "?".
#
# Runaway is held off by other things anyway: a node can only move by calling
# `step.finished` with an outcome from its own enum, `records` holds it until it has
# written down what it found, and the graph has no edge leading backwards.
MAX_NODES_PER_TURN = 8

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
    # What it actually said, kept beside the node that said it.
    #
    # Whether a line was a promise no tool here could keep is the difference between a
    # model fault and a configuration one, and answering it needs to know which node the
    # words came from. That used to be worked out by zipping the steps that spoke against
    # the agent's transcript lines and trusting the two to stay in step. They did, until a
    # turn started joining two nodes' words into one reply — at which point every pairing
    # after it was wrong, and silently.
    text: str = ""


@dataclass
class Turn:
    reply: str
    steps: list[Step] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    # Words said by a step on its way out, before the step that actually answers.
    #
    # "Thanks, noted" from `sizing` and the two service options from `offer_options` are
    # one message to the customer, which is what the prompt promises them: *the next step
    # replies in the same breath*. Held here and joined at the end.
    bridges: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return round(sum(step.seconds for step in self.steps), 1)


class Conversation:
    def __init__(self, world: World, llm: Any, flow: Flow | None = None, *,
                 start_at: str = "", known: dict[str, Any] | None = None,
                 progress: Callable[[str], None] | None = None,
                 on_message: Callable[[str, str], None] | None = None) -> None:
        """`start_at` begins part-way down the graph, with `known` already on the ticket.

        Reaching `booking` from the top costs eight nodes and twenty model calls, and
        nineteen of them are testing nodes that have not failed in days. Starting at the
        node under test is only sound because of how this is built: a node reads the
        ticket, never the transcript, so a ticket carrying the right facts is
        indistinguishable from having walked there. If that ever stops being true, these
        stop being valid — which is itself worth knowing.

        `progress` and `on_message` are how a live channel watches without this knowing
        anything about it. `progress` is called with a tool's dotted name the moment it
        has run, so a widget can say "checking the calendar" instead of showing a minute
        of three dots; `on_message` with (speaker, text) for both sides, so the exchange
        can be written down somewhere durable. Both unset in the harness — nobody is
        waiting there, and the transcript is already being collected.
        """
        self.world = world
        self.llm = llm
        self.progress = progress
        self.on_message = on_message
        self.flow = flow or load(known_tools=sim_tools.names())
        self.node: Node = self.flow[start_at or self.flow.entry]
        # Opened here, not by a tool. It was a tool, and twice out of six the model
        # finished the first step without calling it — after which every set_fields had
        # nowhere to write and the whole conversation kept no record at all. Opening a
        # ticket is bookkeeping, not a decision, and nothing is served by asking.
        self.ticket_id = world.open_ticket().id
        self.entry = self.node.name
        # Kept, not just applied. When somebody comes back a week later the engine opens a
        # fresh ticket, and without this it would be a ticket that knows nothing — so a
        # customer we are talking to *on* their number would be asked for it.
        self.known = dict(known or {})
        if self.known:
            self.world.remember(self.ticket_id, self.known)
        self.messages: list[dict[str, Any]] = []
        self.finished = False

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
        self._said("customer", text)
        self.messages.append({"role": "user", "content": text})

        for _ in range(MAX_NODES_PER_TURN):
            reply = self._run_node(turn)
            if reply is not None:
                turn.reply = reply
                break
            if self.finished:
                break

        # What the steps said on their way out, then what the step that answered said.
        # One message, which is what the customer is promised and what they should see:
        # "Thanks, noted." followed by two service options is one reply, not two turns.
        turn.reply = " ".join([*turn.bridges, turn.reply or ""]).strip()
        self._said("agent", turn.reply)
        return turn

    def _doing(self, tool: str | None) -> None:
        """Tell whoever is watching which tool just ran. Same rule as `_said`: their
        bookkeeping must not be able to end somebody's conversation."""
        if self.progress is None or not tool:
            return
        try:
            self.progress(tool)
        except Exception:  # noqa: BLE001, S110
            pass

    def _said(self, speaker: str, text: str) -> None:
        """Tell whoever is keeping the record. Never lets their bookkeeping end the turn.

        A conversation that dies because the message log was unwritable is a conversation
        lost to something the customer has no stake in.
        """
        if self.on_message is None or not text:
            return
        try:
            self.on_message(speaker, text)
        except Exception:  # noqa: BLE001, S110
            pass

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
                # After it has run, not before. The tools themselves finish instantly; the
                # wait a customer is sitting through is the model call that comes next, so
                # the tool that just ran is the honest thing to have on screen during it.
                self._doing(sim_tools.dotted(call.function.name, node.tools))
                self._absorb(result, keep)
                if isinstance(result, dict):
                    if result.get("finished"):
                        outcome = str(result.get("outcome", ""))
                    if result.get("ok") is False:
                        step.refusals.append(f"{call.function.name}: {result['error']}")
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, default=str)})

            if outcome is not None:
                if (unrecorded := self._unrecorded(node)):  # noqa: SIM102
                    # It says it is done and has not written down what it was here to
                    # find out. Letting it go is how a customer gets asked, four steps
                    # later, something they answered in their first sentence.
                    messages.append({
                        "role": "user",
                        "content": f"[system] Not yet — nothing has been written down for: "
                                   f"{', '.join(unrecorded)}. Call ticket.set_fields with "
                                   f"what they told you, in their words, then finish.",
                    })
                    continue
                said = (message.content or "").strip()
                self._advance(str(outcome))
                if said:
                    # Held, not returned. A reply ends the turn, and a step that is
                    # handing on has by definition not done the thing it is talking
                    # about — so returning its words stops the walk and leaves the
                    # customer with a promise nobody is on their way to keep. "One moment
                    # while I get that sorted" did exactly that: the step that would have
                    # sorted it did not run until they typed "?" to ask if anyone was
                    # there.
                    #
                    # The prompt already tells a step handing on that *the next step
                    # replies in the same breath*. This is that breath.
                    turn.bridges.append(said)
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
        """A fresh conversation, on a fresh ticket, from where this one began.

        The new ticket starts knowing what the channel knew — their number, above all.
        Nothing else carries over: a booked job and a new leak a week later share a
        customer and nothing else, and copying the old ticket's conclusions onto the new
        one would have the second conversation diagnosing the first one's fault.
        """
        self.finished = False
        self.node = self.flow[self.entry]
        self.messages = []
        self.ticket_id = self.world.open_ticket().id
        if self.known:
            self.world.remember(self.ticket_id, self.known)

    # How many replies a step gets before it is asked what it is still waiting for.
    #
    # Two, not three. `warranty_check` looked the old job up on its first call, wrote it
    # down on its second, and then argued with an angry customer for four turns — the
    # nudge landed on the fourth and it finished on the fifth, by which time the customer
    # had asked the same question four ways. A step whose work is done is not made better
    # by another exchange, and the one node here that genuinely needs several rounds
    # (`new_customer`, three fields) is asking questions rather than repeating itself.
    REPLIES_BEFORE_A_NUDGE = 2

    def _still_here(self, node: Node) -> str:
        """A step that keeps talking and never finishes, told so.

        The mirror of `_ends_here`. That one stops a last step signing off before its work
        is done; this one stops every other step doing the opposite — talking indefinitely
        with the way out in front of it. `greeting` spent seventeen model calls
        sympathising with an angry customer, and `offer_options`, cornered by "so is it
        booked?", answered yes. Both had step.finished the whole time.

        A step is not stuck because it lacks patience. It is stuck because what the
        customer is pressing for lives further down the graph, and the only way to reach
        it is to finish.
        """
        if node.is_terminal:
            return ""
        spoke = sum(1 for m in self.messages
                    if m.get("role") == "assistant" and (m.get("content") or "").strip())
        if spoke < self.REPLIES_BEFORE_A_NUDGE:
            return ""
        return (
            f"[system] You have replied {spoke} times from this step without calling "
            f"step.finished. If this step's goal is met, call it now. If you are waiting "
            f"on something this step has no tool for, a later step has it — finishing is "
            f"how they get it, and repeating yourself is not."
        )

    # Bookkeeping, not work: writing a field down is not doing the thing.
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

    def _unrecorded(self, node: Node) -> list[str]:
        """The fields this node had to write and has not. See `Node.records`.

        Read off the ticket rather than off which tools were called: what matters is that
        the fact is there, and it does not matter whether this node wrote it, an earlier
        one did, or a tool's `remembers` put it there without anybody being asked.
        """
        tags = self.tags
        return [name for name in node.records
                if tags.get(name) in (None, "", [], {})]

    def _undone(self, node: Node, turn: Turn) -> list[str]:
        """The node's own tools it has not used yet, in this node, this conversation."""
        used = {tool for step in turn.steps if step.node == node.name for tool in step.tools}
        return [
            name for name in node.tools
            if name not in self.NOT_WORK and name.replace(".", "_", 1) not in used
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
            self.world.remember(self.ticket_id, keep)

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
        self.world.remember(self.ticket_id, {memory.NODE_TAG: self.node.name})
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

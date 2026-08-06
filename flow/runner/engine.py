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
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from flow.runner import assemble, memory
from flow.runner.graph import Flow, Node, load
from flow.sim import tools as sim_tools
from flow.sim.world import World

# One customer message must not be able to spend the afternoon. Generous enough that a
# node asking two questions and calling three tools is nowhere near it.
MAX_CALLS_PER_TURN = 8
# A node that finishes without saying anything hands straight on to the next. More than a
# few of those in one turn means the graph is walking itself, which is worth stopping.
MAX_NODES_PER_TURN = 4

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


@dataclass
class Turn:
    reply: str
    steps: list[Step] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return round(sum(step.seconds for step in self.steps), 1)


class Conversation:
    def __init__(self, world: World, llm: Any, flow: Flow | None = None) -> None:
        self.world = world
        self.llm = llm
        self.flow = flow or load(known_tools=sim_tools.names())
        self.node: Node = self.flow[self.flow.entry]
        # Opened here, not by a tool. It was a tool, and twice out of six the model
        # finished the first step without calling it — after which every set_fields had
        # nowhere to write and the whole conversation kept no record at all. Opening a
        # ticket is bookkeeping, not a decision, and nothing is served by asking.
        self.ticket_id = world.open_ticket().id
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
        self.messages.append({"role": "user", "content": text})

        for _ in range(MAX_NODES_PER_TURN):
            reply = self._run_node(turn)
            if reply is not None:
                turn.reply = reply
                return turn
            if self.finished:
                break
        turn.reply = turn.reply or ""
        return turn

    # ------------------------------------------------------------------
    def _run_node(self, turn: Turn) -> str | None:
        """Work the current node. Returns what to say, or None if it moved on silently."""
        node = self.node
        turn.nodes.append(node.name)
        messages = [{"role": "system", "content": self._system()}, *self.messages]

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
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, default=str)})

            if outcome is not None:
                said = (message.content or "").strip()
                self._advance(str(outcome))
                return said or None

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
        """A fresh conversation, on a fresh ticket, from the top of the graph."""
        self.finished = False
        self.node = self.flow[self.flow.entry]
        self.messages = []
        self.ticket_id = self.world.open_ticket().id

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

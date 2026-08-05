"""One real conversation with one real customer, advanced a message at a time.

The orchestrator in `plumbing.orchestrator` drives the loop: it asks a simulated customer
for the next thing they say. In production that is inverted — a person types, an HTTP
request arrives, and the agent owes them one reply. The mechanics are the same (the same
`Agent.respond`, the same handoff rules), so this holds the same state between requests
rather than reimplementing any of it.

The orchestrator is left alone deliberately. The whole test rig runs through it, and a
production concern leaking into the thing that decides whether the tests can be believed
is how a harness stops being trustworthy.

**What survives a restart.** Customers, tickets and appointments do — they are in the
database. An in-flight conversation does not: the message list lives in memory here. A
restart mid-chat means the next message starts a fresh conversation against the same
customer record, which reads as the agent losing the thread but never as losing the job.
Worth fixing when there is traffic to justify it; not worth pretending it is already done.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from plumbing.agent_registry import Agent
from plumbing.llm import LLM
from plumbing.orchestrator import _handoff_briefing, Transcript
from plumbing.tools.registry import ToolContext


@dataclass
class LiveConversation:
    """A conversation in progress. One per customer, held between HTTP requests."""

    agents: dict[str, Agent]
    entry_agent: str
    llm: LLM
    ctx: ToolContext
    channel: str                      # chat | sms | voice | telegram
    phone: str = ""
    session_id: str = ""

    active_name: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    involved: list[str] = field(default_factory=list)
    handoffs: int = 0
    transcript: Transcript = field(default_factory=Transcript)
    closed: bool = False

    def __post_init__(self) -> None:
        self.active_name = self.active_name or self.entry_agent
        if not self.messages:
            self.messages = self.agents[self.active_name].initial_messages()
            self.involved = [self.active_name]
            if self.phone:
                self.messages.append({"role": "user", "content": self._number_already_known()})

    def _number_already_known(self) -> str:
        """Every live channel hands us the number before the customer says anything.

        The shared rules open by asking for it, which is right when nothing supplies it and
        wrong here: asking somebody who typed their number into the form thirty seconds ago,
        or who is speaking to us *on* the number, is the clearest possible sign that nobody
        is listening. So the agent is told at the top of the conversation, before the first
        customer message, and the instruction to look it up immediately still applies.

        How much the number is worth differs by channel and the agent is told which: a
        carrier vouches for SMS and voice, whereas a chat number is typed and could be
        anybody's. That does not change what the agent does — the gate on acting without a
        number is code either way — but it is the truth about the identity, and an agent
        that believes a typed number is proven will say so to the customer.
        """
        source = (
            "they are contacting us from it, so the carrier vouches for it"
            if self.channel in {"sms", "voice"}
            else "they typed it in to open the chat, so it is their claim and not proof"
        )
        return (
            f"[system] This customer's phone number is already known: {self.phone} — "
            f"{source}. Do not ask them for it. Call crm.lookup_by_phone with it now, "
            f"before you reply to them."
        )

    # ------------------------------------------------------------------
    @property
    def store(self) -> Any:
        return self.ctx.world.store

    def say(self, text: str) -> str:
        """Feed one customer message and return what the agent says back.

        Runs the agent until it produces something to send. A handoff or a rejected tool
        call is not a reply, so those loop; the ceiling stops a wedged agent from spending
        the customer's afternoon and our money.
        """
        if self.closed:
            # A closed process reopens as a new one rather than resurrecting the old
            # thread: the ticket is closed, and continuing to append to it would put new
            # work on a finished record.
            self._restart()

        self._record("customer", text)
        self.messages.append({"role": "user", "content": text})

        for _ in range(self.llm.limit("max_tool_calls_per_turn", 15)):
            turn = self.agents[self.active_name].respond(self.ctx, self.messages)

            if turn.handoff and self._switch_to(turn.handoff):
                continue

            if turn.ended:
                self.closed = True
                if turn.reply:
                    self._record("agent", turn.reply)
                    return turn.reply
                return ""

            if turn.reply:
                self._record("agent", turn.reply)
                return turn.reply

            self.messages.append({
                "role": "user",
                "content": "[system] You said nothing to the customer. Reply in plain text, "
                           "or call conversation.end if the process really is closed.",
            })

        return (
            "Sorry — I am having trouble getting that sorted. Let me pass this to a "
            "colleague who will come back to you."
        )

    # ------------------------------------------------------------------
    def _switch_to(self, handoff: dict[str, Any]) -> bool:
        target = handoff["to_agent"]
        if target not in self.agents:
            # Only the agents this deployment has turned on exist. Being asked for one
            # that is off is not an error the customer should see — the agent is told what
            # it may actually reach and picks again.
            self.messages.append({
                "role": "user",
                "content": f"[system] '{target}' is not available in this deployment. "
                           f"You can transfer to: {sorted(self.agents)}. If none of them "
                           f"fit, take the details and use escalate.raise instead.",
            })
            return True

        self.handoffs += 1
        if self.handoffs > self.llm.limit("max_handoffs", 5):
            self.messages.append({
                "role": "user",
                "content": "[system] Too many transfers. Handle it yourself or escalate.",
            })
            return True

        self.active_name = target
        self.involved.append(target)
        self.ctx.conversation_ended = False
        self.messages = self.agents[target].initial_messages()
        self.messages.append({
            "role": "user",
            "content": _handoff_briefing(handoff, self.transcript, self.ctx.world.active_ticket_id),
        })
        return True

    def _restart(self) -> None:
        self.closed = False
        self.handoffs = 0
        self.active_name = self.entry_agent
        self.involved = [self.entry_agent]
        self.ctx.conversation_ended = False
        self.ctx.world.active_ticket_id = ""
        self.messages = self.agents[self.entry_agent].initial_messages()

    def _record(self, speaker: str, text: str) -> None:
        self.transcript.add(speaker, text)
        if self.store is None:
            return
        self.store.add_message(
            channel=self.channel, speaker=speaker, text=text,
            phone=self.phone, session_id=self.session_id,
            ticket_id=self.ctx.world.active_ticket_id or "",
        )

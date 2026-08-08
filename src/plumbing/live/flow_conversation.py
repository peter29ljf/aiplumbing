"""One real conversation with one real customer, walking the flow graph.

The engine in `flow.runner.engine` is written for the harness, where a simulated customer
is driven in a loop and everything lives in memory. In production it is inverted — a person
types, an HTTP request arrives, and we owe them one reply — so this holds the engine's state
between requests and wires it to the things production has and the harness does not: a
database to write the exchange into, and somebody sitting there waiting who should be told
what is happening.

The engine itself is not adapted. It is the same object the scenarios run against, over the
same tools with the same prompts; only the world underneath is different (`flow/live/world.py`
instead of `flow/sim/world.py`). That is the whole reason for the split — a production path
with its own copy of any of it would be a path nobody had tested.

**What survives a restart.** Customers, tickets, appointments, follow-ups and every message
do — they are rows. The place in the graph does not: `flow_node` is written to the ticket,
but the engine's message list is here in memory. A restart mid-chat means the next message
starts a fresh conversation against the same customer, which reads as losing the thread and
never as losing the job.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from flow.live.world import LiveWorld
from flow.runner.engine import Conversation

# What to send when a turn produces no words at all.
#
# It happens: a step finishes silently, the next one finishes silently too, and the turn
# ends having moved the conversation along without anybody speaking. `always.md` says say
# something every turn and the terminal-step guard makes a last node speak, but the widget
# cannot tell an empty reply from a failure — it tells the customer we could not be
# reached, sometimes on the very turn we did the whole job.
#
# **Two wordings, because the two cases are opposite.** The first version had only the
# sign-off, and it went out in the middle of a booking: the customer was told everything
# was in hand while the conversation carried on around them, which is exactly the "you are
# all set" lie the rest of this system is built to prevent. A filler must never be able to
# say more than the truth, and mid-conversation the truth is only that we are still here.
STILL_GOING = "Bear with me one moment — I'm just getting that sorted for you."
NOTHING_MORE_TO_SAY = (
    "That's all in hand at our end — thanks for getting in touch. "
    "If anything else comes up, just message us again."
)


class FlowConversation:
    """A conversation in progress. One per customer, held between HTTP requests."""

    def __init__(self, *, store: Any, llm: Any, channel: str, phone: str = "",
                 session_id: str = "", flow: Any = None) -> None:
        self.store = store
        self.channel = channel                 # chat | sms | voice
        self.phone = phone
        self.session_id = session_id
        self.world = LiveWorld(store)
        # The number goes on the ticket before the first word, so `identify` reads it off
        # the summary rather than asking. Every live channel has one: a carrier supplies it
        # on SMS and voice, and the widget takes it on the form that opens the chat.
        #
        # Whether it is *proven* is deliberately not carried. A carrier vouches for a
        # number and a typed one is only a claim, which was worth saying when a gate
        # depended on it; nothing in this graph does, and a tag on the ticket is a line in
        # all seventeen node prompts for a distinction none of them act on.
        self.talk = Conversation(
            self.world, llm, flow,
            known={"phone": phone} if phone else None,
            on_message=self._record,
        )

    # ------------------------------------------------------------------
    @property
    def progress(self) -> Callable[[str], None] | None:
        """Called with each tool's dotted name as it runs, for whoever is waiting."""
        return self.talk.progress

    @progress.setter
    def progress(self, watcher: Callable[[str], None] | None) -> None:
        self.talk.progress = watcher

    @property
    def closed(self) -> bool:
        return self.talk.finished

    @property
    def ticket_id(self) -> str:
        return self.talk.ticket_id

    @property
    def node(self) -> str:
        """Which step of the graph answered last. For the console and the logs."""
        return self.talk.node.name

    def say(self, text: str) -> str:
        """Feed one customer message and return what to send them."""
        turn = self.talk.say(text)
        if turn.reply:
            return turn.reply

        # Nothing came back, so a filler goes out — and it is written down like any other
        # reply. The engine only reports what it produced, so leaving this unrecorded gave
        # a transcript where the customer answers a message nobody sent, and the archive of
        # what we told them was missing something we told them.
        filler = NOTHING_MORE_TO_SAY if self.closed else STILL_GOING
        self._record("agent", filler)
        return filler

    # ------------------------------------------------------------------
    def _record(self, speaker: str, text: str) -> None:
        """Every line, both directions, filed against the customer and the ticket.

        The engine drops a node's messages when the flow moves on — that is where the
        context saving comes from — so this is the only place the conversation exists in
        full afterwards. Handed to the engine as `on_message`, which swallows anything
        raised here: a customer must not lose their answer because a write failed.
        """
        self.store.add_message(
            channel=self.channel, speaker=speaker, text=text,
            phone=self.phone, session_id=self.session_id,
            ticket_id=self.talk.ticket_id or "",
        )

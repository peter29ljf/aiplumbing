"""Who is talking, and where their conversation was when they last said something.

This is the whole inbound seam, and it is short because the engine already has the shape it
needs. The previous generation spent three hundred lines here for one reason: its engine
was driven by a loop that asked a simulated customer for the next line, and a person typing
into a web page is the opposite arrangement — a request arrives, one reply is owed, and
everything else has to be held. `LiveConversation` existed to perform that inversion.

`Conversation.say(text) -> Turn` is already that shape. So what is left is the part nobody
had written: **where the conversation lives between two messages.**

In memory, and in the database behind it. The dictionary is the fast path for somebody
typing; the database is what makes a deploy, a crash or a customer who wandered off for
twenty minutes survivable. Without it the seventeen-node walk lives in one process's
memory, and a restart at the wrong moment asks a customer who has already given their name,
their address, their fault and a time for all of it again from the top.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from bat.live.store import SqliteStore
from bat.live.world import LiveWorld
from bat.runtime import project as projects
from bat.runtime import registry
from bat.runtime.engine import Conversation
from bat.runtime.graph import load
from bat.runtime.llm import LLM



class Sessions:
    """One per server. Holds the flow and the database; hands out conversations."""

    def __init__(self, project: str, database: str | Path, *,
                 supervisor: str = "", llm: Any = None) -> None:
        self.project = projects.find(project)
        self.store = SqliteStore(database)
        self.supervisor = supervisor
        self.rules = self.project.business_rules()
        # Loaded once. Reading and validating the graph per message would put a file read
        # and a full check in front of every customer, and a flow that changed under a
        # running conversation is a conversation whose next step may not exist.
        self.flow = load(self.project, known_tools=registry.load_tools(self.project))
        self._llm = llm or LLM(self.project.model())
        self._live: dict[str, Conversation] = {}
        # One turn at a time per session, and one dictionary shared by every thread. The
        # server refuses a second message while one is running, so this guards the lookup
        # rather than the model call.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def get(self, session_id: str, phone: str = "") -> Conversation:
        """This session's conversation: the one in memory, the one in the database, or new.

        The order matters. Something said a minute ago is only in memory until the turn
        that said it finishes, and the fresher of the two is always the right one.
        """
        with self._lock:
            found = self._live.get(session_id)
            if found is not None:
                return found
            saved = self.store.load_conversation(session_id)
            talk = self._resume(session_id, saved) if saved else self._new(session_id, phone)
            self._live[session_id] = talk
            return talk

    def save(self, session_id: str, talk: Conversation, phone: str = "") -> None:
        """Written after every turn, not at the end.

        There is no end. A conversation is abandoned far more often than it is finished,
        and the one that matters most to survive a restart is the one in the middle.
        """
        self.store.save_conversation(
            session_id, talk.save(), phone=phone, project=self.project.name,
            node=talk.node.name, finished=talk.finished,
        )

    def close(self, session_id: str) -> None:
        """Let go of the memory copy. The database keeps the conversation."""
        with self._lock:
            self._live.pop(session_id, None)

    # ------------------------------------------------------------------
    def _world(self, session_id: str) -> LiveWorld:
        from datetime import datetime

        return LiveWorld(
            now=datetime.now().astimezone().isoformat(),
            rules=self.rules, store=self.store, session_id=session_id,
            records=self.project.records(), supervisor=self.supervisor,
        )

    def _new(self, session_id: str, phone: str) -> Conversation:
        world = self._world(session_id)
        talk = Conversation(world, self._llm, self.flow)
        if phone:
            # The number the customer typed into the form, on the ticket before the first
            # word. The lookup step asks for it anyway and will get it from here — being
            # asked for something you have already given is the clearest possible sign
            # nobody is listening.
            ticket = world.ticket(talk.ticket_id)
            ticket.phone = phone
            ticket.tags["phone"] = phone
        return talk

    def _resume(self, session_id: str, saved: dict[str, Any]) -> Conversation:
        """The world first, with its store attached, then the conversation on top of it.

        The world is handed in rather than left to `Conversation.resume`, which builds a
        plain simulated one. A live conversation restored that way looks entirely correct
        and reaches nobody.
        """
        world = LiveWorld.restore(
            saved["world"], rules=self.rules, store=self.store, session_id=session_id,
            records=self.project.records(), supervisor=self.supervisor,
        )
        return Conversation.resume(saved, self._llm, self.flow, rules=self.rules,
                                   world=world)

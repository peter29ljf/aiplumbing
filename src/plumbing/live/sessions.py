"""Finding the conversation a message belongs to.

There used to be a second decision here — which of five agents this deployment switched on
— and there is no longer anything to switch. The flow is one graph with one way in, and a
node that has not earned real customers is not turned off, it is a node the conversation
does not reach. `config/live.yaml` keeps only the browser origins that may call the chat.

Identity still differs by channel, and it still matters:

- **sms** and **voice** arrive with a number the carrier vouches for.
- **chat** arrives from the anonymous internet with nothing. The number is typed into a
  form and is a *claim*: the same digits could be anybody's. So a chat session is tracked
  by its session id, and the number it asserts is carried alongside rather than trusted as
  identity.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from plumbing.live.flow_conversation import FlowConversation
from plumbing.llm import LLM
from plumbing.store import SqliteStore, phone_key
from plumbing import config


class SessionStore:
    """In-memory conversations over a durable world.

    Conversations are keyed by phone for the channels that have one and by session id for
    chat. A restart drops what people were part-way through saying; it does not drop a
    customer, a ticket or a booking, which are in the database.
    """

    def __init__(self, database_path: str, llm: LLM | None = None) -> None:
        self.store = SqliteStore(database_path)
        from plumbing.live.offers import Offers  # noqa: PLC0415

        self.offers = Offers(self.store)
        self.llm = llm or LLM()
        self._sessions: dict[str, FlowConversation] = {}
        self._lock = threading.Lock()
        # Loaded once and shared. The graph is read-only after `load()` and validating it
        # per conversation would read eighteen files to reach the same answer — and would
        # let a broken edit take down the next customer rather than the next restart.
        self._flow = _graph()

    # ------------------------------------------------------------------
    def key_for(self, *, channel: str, phone: str = "", session_id: str = "") -> str:
        """One conversation per person, not per channel.

        Someone who texts and then opens the web chat with the same number is in one
        conversation, because they are one customer with one problem.
        """
        if phone and phone_key(phone):
            return f"phone:{phone_key(phone)}"
        if session_id:
            return f"session:{session_id}"
        return f"anon:{uuid.uuid4()}"

    def get(self, *, channel: str, phone: str = "", session_id: str = "") -> FlowConversation:
        key = self.key_for(channel=channel, phone=phone, session_id=session_id)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                # Channel can change mid-conversation — they text, then use chat. Later
                # messages are filed under where they actually arrived.
                existing.channel = channel
                if phone and not existing.phone:
                    existing.phone = phone
                return existing
            conversation = FlowConversation(
                store=self.store, llm=self.llm, channel=channel,
                phone=phone, session_id=session_id, flow=self._flow,
            )
            self._sessions[key] = conversation
            return conversation

    def technician_by_chat_id(self, chat_id: str) -> dict[str, Any] | None:
        """Only people on the roster may drive the bot."""
        for spec in config.world_seed().get("technicians", []):
            if str(spec.get("telegram_chat_id") or "") == chat_id:
                return spec
        return None

    def record_technician_message(self, *, chat_id: str, text: str) -> None:
        """Kept with everything else that was said, so the thread survives a restart."""
        self.store.add_message(channel="telegram", speaker="technician", text=text,
                               session_id=f"telegram:{chat_id}")
        self.store.add_event("technician_replied", detail=text[:200], chat_id=chat_id)

    def end(self, *, phone: str = "", session_id: str = "") -> None:
        with self._lock:
            self._sessions.pop(self.key_for(channel="", phone=phone, session_id=session_id), None)


def _graph() -> Any:
    """The flow, validated against the tools that actually exist.

    `load` refuses a graph that does not hang together — a branch naming a node nobody
    wrote, a rules file renamed in one place only, a tool that is gone. Doing it here means
    a broken edit fails when the server starts, not in front of whoever messages next.
    """
    from flow.runner.graph import load  # noqa: PLC0415
    from flow.sim import tools  # noqa: PLC0415

    return load(known_tools=tools.names())

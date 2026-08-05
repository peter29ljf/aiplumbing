"""Which agents are switched on, and finding the conversation a message belongs to.

**Only intake and small_job go live first.** Everything else — a warranty claim, a large
project, anything urgent — is taken down as a message and handed to a person. Those flows
work in the simulator and have not earned real customers yet, and the money-losing paths
(deposits, refunds, dispatch) all live in the ones that stay off. Turning them on later is
a line in `config/live.yaml`, not a code change.

Identity is the other thing decided here, and it differs by channel:

- **sms** and **voice** arrive with a number the carrier vouches for.
- **chat** arrives from the anonymous internet with nothing. The number is typed into a
  form and is a *claim*, not proof — the same digits could be anybody's. So a chat session
  is tracked by its session id, and the phone number it asserts is carried alongside
  rather than trusted as identity.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from plumbing import agent_registry, config
from plumbing.live.conversation import LiveConversation
from plumbing.llm import LLM
from plumbing.store import SqliteStore, phone_key
from plumbing.tools.registry import ToolContext
from plumbing.world import World

# Agents this deployment will actually route to. An agent left out is not merely unused:
# `LiveConversation` tells the model it cannot be reached, so intake takes the details and
# escalates instead of transferring into silence.
ENABLED_AGENTS = ("intake", "small_job")

ENTRY_AGENT = "intake"


class SessionStore:
    """In-memory conversations over a durable world.

    Conversations are keyed by phone for the channels that have one and by session id for
    chat. A restart drops what people were part-way through saying; it does not drop a
    customer, a ticket or a booking, which are in the database.
    """

    def __init__(self, database_path: str, llm: LLM | None = None) -> None:
        self.store = SqliteStore(database_path)
        self.llm = llm or LLM()
        self._sessions: dict[str, LiveConversation] = {}
        self._lock = threading.Lock()

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

    def get(self, *, channel: str, phone: str = "", session_id: str = "") -> LiveConversation:
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
            conversation = self._build(channel=channel, phone=phone, session_id=session_id)
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

    # ------------------------------------------------------------------
    def _build(self, *, channel: str, phone: str, session_id: str) -> LiveConversation:
        from datetime import datetime  # noqa: PLC0415

        world = World(datetime.now().astimezone().isoformat(), store=self.store)
        agents = _enabled_agents(self.llm)
        # Each Agent resolves its own allow-list; the context only carries the world.
        ctx = ToolContext(world=world)
        return LiveConversation(
            agents=agents, entry_agent=ENTRY_AGENT, llm=self.llm, ctx=ctx,
            channel=channel, phone=phone, session_id=session_id,
        )


def _enabled_agents(llm: LLM) -> dict[str, Any]:
    everything = agent_registry.build_all(llm)
    missing = [name for name in ENABLED_AGENTS if name not in everything]
    if missing:
        raise RuntimeError(f"config/agents.yaml has no agent named {missing}")
    return {name: everything[name] for name in ENABLED_AGENTS}

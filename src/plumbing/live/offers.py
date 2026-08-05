"""Offering a job to the technician, and getting a yes or a no back.

    ┌──────────────────────────────────────────┐
    │ New job — Wed 6 Aug, 9:00 am             │
    │ Dripping laundry tap                     │
    │ 104 - 15288 36 Ave, Surrey               │
    │ Linda Zhang · 604-721-8629               │
    ├──────────────────────────────────────────┤
    │   [ ✅ Accept ]      [ ❌ Decline ]       │
    └──────────────────────────────────────────┘

**Accept is one tap.** Nothing else is asked, because there is nothing else to know and
the technician is usually holding something.

**Decline needs a reason**, because "no" on its own leaves the office with a customer
expecting somebody and no idea what to tell them. Two ways to give it, and both work:

- Type the reason, then tap Decline. The message typed since the offer went out is taken
  as the reason.
- Tap Decline first. The bot asks why, and the next thing typed is the reason.

The second exists because the first will not always happen. A technician who taps the
obvious button before reading the instruction is not making a mistake — the interface is.

State lives in the database rather than in memory: an offer that a restart forgets is a
job nobody is going to, and the technician gets no second prompt because as far as the
bot is concerned it never asked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Telegram caps callback_data at 64 bytes and rejects the message outright when it is
# over, so what travels on the button is a code, not the job.
ACCEPT = "a"
DECLINE = "d"


def callback_data(offer_id: str, decision: str) -> str:
    return f"o:{offer_id}:{decision}"


def parse_callback(data: str) -> tuple[str, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "o" or parts[2] not in (ACCEPT, DECLINE):
        return None
    return parts[1], parts[2]


@dataclass
class Offer:
    offer_id: str
    ticket_id: str
    chat_id: str
    summary: str
    state: str = "sent"          # sent | accepted | declined | awaiting_reason
    reason: str = ""
    message_id: str = ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    offer_id   TEXT PRIMARY KEY,
    ticket_id  TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL DEFAULT 'sent',
    reason     TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_chat ON offers(chat_id, state);
"""


class Offers:
    """Job offers and their answers, kept next to everything else about the ticket."""

    def __init__(self, store: Any) -> None:
        self.store = store
        with store.connect() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------
    def create(self, *, ticket_id: str, chat_id: str, summary: str) -> Offer:
        offer_id = self.store.next_id("OF")
        now = _now()
        with self.store.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO offers (offer_id, ticket_id, chat_id, summary, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?)",
                (offer_id, ticket_id, chat_id, summary, now, now),
            )
        return Offer(offer_id=offer_id, ticket_id=ticket_id, chat_id=chat_id, summary=summary)

    def get(self, offer_id: str) -> Offer | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
            ).fetchone()
        return _to_offer(row) if row else None

    def awaiting_reason(self, chat_id: str) -> Offer | None:
        """The offer this technician has been asked to explain, if any."""
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM offers WHERE chat_id = ? AND state = 'awaiting_reason' "
                "ORDER BY updated_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return _to_offer(row) if row else None

    def open_offer(self, chat_id: str) -> Offer | None:
        """The most recent offer still waiting on an answer."""
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM offers WHERE chat_id = ? AND state IN ('sent','awaiting_reason') "
                "ORDER BY updated_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return _to_offer(row) if row else None

    # ------------------------------------------------------------------
    def set_message_id(self, offer_id: str, message_id: str) -> None:
        self._update(offer_id, message_id=message_id)

    def accept(self, offer_id: str) -> None:
        self._update(offer_id, state="accepted")
        self._event(offer_id, "job_accepted")

    def decline(self, offer_id: str, reason: str) -> None:
        self._update(offer_id, state="declined", reason=reason)
        self._event(offer_id, "job_declined", detail=reason)

    def ask_for_reason(self, offer_id: str) -> None:
        self._update(offer_id, state="awaiting_reason")

    def note_typed(self, chat_id: str, text: str) -> None:
        """Remember what was typed while an offer is open.

        This is what makes "type the reason, then tap Decline" work: the message is not
        an answer to anything yet, so it is held against the open offer until a button
        settles it.
        """
        offer = self.open_offer(chat_id)
        if offer is not None and offer.state == "sent":
            self._update(offer.offer_id, reason=text)

    # ------------------------------------------------------------------
    def _update(self, offer_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.store.connect(write=True) as conn:
            conn.execute(
                f"UPDATE offers SET {sets}, updated_at = ? WHERE offer_id = ?",
                (*fields.values(), _now(), offer_id),
            )

    def _event(self, offer_id: str, kind: str, detail: str = "") -> None:
        offer = self.get(offer_id)
        if offer is None:
            return
        self.store.add_event(kind, ticket_id=offer.ticket_id, detail=detail, offer_id=offer_id)


def _to_offer(row: Any) -> Offer:
    return Offer(
        offer_id=row["offer_id"], ticket_id=row["ticket_id"], chat_id=row["chat_id"],
        summary=row["summary"], state=row["state"], reason=row["reason"],
        message_id=row["message_id"],
    )


def _now() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().astimezone().isoformat(timespec="seconds")


def buttons(offer_id: str) -> list[list[dict[str, str]]]:
    return [[
        {"text": "✅ Accept", "data": callback_data(offer_id, ACCEPT)},
        {"text": "❌ Decline", "data": callback_data(offer_id, DECLINE)},
    ]]


def offer_text(summary: str) -> str:
    return f"<b>New job</b>\n\n{summary}\n\nAccept, or tell me why not."


def settled_text(offer: Offer) -> str:
    """What the message becomes once answered, so the buttons cannot be pressed again."""
    if offer.state == "accepted":
        return f"<b>New job — accepted ✅</b>\n\n{offer.summary}"
    return f"<b>New job — declined ❌</b>\n\n{offer.summary}\n\n<i>Reason: {offer.reason}</i>"

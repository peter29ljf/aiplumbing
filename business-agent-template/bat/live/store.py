"""Durable storage for the things that must outlive a conversation.

Until now every conversation built a `World` from `config/world_seed.yaml` and threw it
away at the end. That is right for tests — each scenario wants a clean, known world — and
useless in production, where a returning customer must be found, a ticket must still be
open tomorrow, and an appointment must still be in the diary when the technician goes out.

So the store is **optional**. A `World` without one behaves exactly as it always has, which
is what the whole test suite depends on. A `World` with one loads from the database instead
of the seed, and writes through on every change.

Only what the seventeen nodes need is here: customers with their past jobs, tickets,
appointments, the message history, follow-ups, chat sessions, the conversations themselves,
the idempotency ledger, and an append-only event log. Payments and warranty reviews belong
to flows that are not going live yet, and **a table nobody writes to is a table that will
be wrong by the time somebody does**.

Lifted whole from the first generation's `src/plumbing/store.py`, which ran in production.
What changed is the field names it reads off a ticket and an appointment, because the two
generations name them differently, and two tables it never had: `conversations`, so a chat
survives a restart, and `ledger`, so an already-sent text is not sent again.

Three things that generation in turn took from a sibling project, whose comments read like
they were paid for in production incidents:

- **WAL, short connections, no process-wide lock.** A global lock there capped the whole
  app at one query at a time, and calls, texts and chat threads queued behind each other.
- **`BEGIN IMMEDIATE` for read-then-write.** A deferred transaction reads at a snapshot and
  asks for the write lock later; under WAL that upgrade fails from a stale snapshot, stalls
  for the whole busy timeout and then raises "database is locked". Ticket status changes are
  exactly that shape.
- **A phone key of the last ten digits.** An inbound `+1 (604) 721-8629` has to match a
  stored `6047218629` without normalising the whole table on every lookup.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

BUSY_TIMEOUT_MS = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    phone       TEXT PRIMARY KEY,
    -- Last ten digits, so a lookup does not have to normalise every stored row.
    phone_key   TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL DEFAULT '',
    area        TEXT NOT NULL DEFAULT '',
    property_type TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_phone_key ON customers(phone_key);

-- Work we have already done for a customer. Read when someone claims a warranty, so it
-- outlives the ticket that created it.
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    phone        TEXT NOT NULL REFERENCES customers(phone),
    service_type TEXT NOT NULL DEFAULT '',
    service_name TEXT NOT NULL DEFAULT '',
    address      TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    technician_id TEXT,
    status       TEXT NOT NULL DEFAULT 'completed',
    amount       REAL,
    warranty_excluded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_phone ON jobs(phone);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id   TEXT PRIMARY KEY,
    phone       TEXT NOT NULL DEFAULT '',
    -- Same last-ten-digits key as customers. Joining these two tables on the raw phone
    -- string silently matched nothing: the ticket carried "+16047218629" while the
    -- customer row held "+1 (604) 721-8629", and every open ticket vanished from the
    -- lookup. Any table holding a phone number carries the key.
    phone_key   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,
    owner_agent TEXT NOT NULL DEFAULT '',
    -- The agent's own notes: property_type, category, issue, address. The apartment gate
    -- reads property_type and category straight out of here.
    tags        TEXT NOT NULL DEFAULT '{}',
    history     TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_phone ON tickets(phone_key);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    ticket_id   TEXT NOT NULL,
    phone       TEXT NOT NULL DEFAULT '',
    -- The same last-ten-digits key as customers and tickets. Somebody ringing to move a
    -- visit booked last week is on a new ticket in a new process, and this is the only
    -- thing that connects the two. See the note on `tickets.phone_key` for what happens
    -- when a table holding a phone number does not carry one.
    phone_key   TEXT NOT NULL DEFAULT '',
    technician_id TEXT,
    start_at    TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    address     TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'booked',
    -- Set once the booking exists in the real calendar too, so a failed sync is visible
    -- rather than silently leaving a technician with nothing in their diary.
    calendar_event_id TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appointments_start ON appointments(start_at);
CREATE INDEX IF NOT EXISTS idx_appointments_ticket ON appointments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments(phone_key);

-- Everything anybody said, whichever channel it came in on. The customer record and the
-- conversation are the same customer's information, so they live in one place.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL DEFAULT '',
    phone_key   TEXT NOT NULL DEFAULT '',
    ticket_id   TEXT NOT NULL DEFAULT '',
    -- chat has no phone number of its own, so it carries a session id instead.
    session_id  TEXT NOT NULL DEFAULT '',
    channel     TEXT NOT NULL,          -- chat | sms | voice | telegram
    speaker     TEXT NOT NULL,          -- customer | agent | technician
    text        TEXT NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone_key, at);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, at);

-- Work the office owes somebody later: the day-after check with the technician on a job
-- that was booked. The agent schedules these during a conversation that then ends, so
-- they cannot live in the conversation — a follow-up that only exists in memory fires
-- exactly never, which is what happened before this table existed.
CREATE TABLE IF NOT EXISTS followups (
    followup_id TEXT PRIMARY KEY,
    ticket_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- job_outcome
    chat_id     TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    due_at      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | asked | answered | given_up
    answer      TEXT NOT NULL DEFAULT '',
    asked_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(status, due_at);

-- A web chat session and the number the person typed to open it. SMS and voice get the
-- number from the carrier on every single request; chat gets it once, in a form, and then
-- sends nothing but text. Holding it in memory would mean a restart makes every open
-- widget demand the number again mid-sentence, which reads as the site losing the
-- conversation. The number is still only a claim — that is what `asserted` records.
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id  TEXT PRIMARY KEY,
    phone       TEXT NOT NULL,
    phone_key   TEXT NOT NULL,
    asserted    TEXT NOT NULL DEFAULT 'typed',   -- nothing vouches for it
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_phone ON chat_sessions(phone_key);

-- A conversation, mid-flight. `Conversation.save()` is one JSON blob holding the world,
-- the node it is standing in, the ticket and the messages so far.
--
-- Without this the whole seventeen-node walk lives in one process's memory. A deploy at
-- the wrong moment, and a customer who has given their name, their address, their fault
-- and picked a time is asked for all of it again from the top.
CREATE TABLE IF NOT EXISTS conversations (
    session_id  TEXT PRIMARY KEY,
    phone_key   TEXT NOT NULL DEFAULT '',
    project     TEXT NOT NULL DEFAULT '',
    node        TEXT NOT NULL DEFAULT '',
    finished    INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_phone ON conversations(phone_key);

-- What has already been done to the outside world, and what it answered.
--
-- `registry.call` keeps this in memory for a scenario, which is the whole life of a
-- scenario. Here it has to outlive the process: the row goes in *before* the call and the
-- answer overwrites it, so a row still reading `__unconfirmed__` after a restart means the
-- send may have landed and nobody knows. It is never repeated — a person checks.
CREATE TABLE IF NOT EXISTS ledger (
    session_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    result      TEXT NOT NULL,
    at          TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);

-- Append-only. Nothing is ever updated or deleted here: when a booking is wrong, the
-- question is always what happened in what order, and a mutable log cannot answer it.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id, at);
"""


def phone_key(phone: str) -> str:
    """Last ten digits. `+1 (604) 721-8629` and `6047218629` are the same customer."""
    return re.sub(r"\D", "", phone or "")[-10:]


class SqliteStore:
    """Durable state for one business. Cheap to construct; holds no connection open."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------
    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """A short-lived connection per operation, committed on the way out.

        `write=True` takes the write lock before reading. Use it for anything that reads a
        row and then writes it back; see the module docstring for why deferred transactions
        fail under WAL.
        """
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        if write:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- customers ---------------------------------------------------
    def find_customer(self, phone: str) -> dict[str, Any] | None:
        key = phone_key(phone)
        if not key:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM customers WHERE phone_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            customer = dict(row)
            customer["jobs"] = [
                dict(j)
                for j in conn.execute(
                    "SELECT * FROM jobs WHERE phone = ? ORDER BY completed_at DESC",
                    (customer["phone"],),
                )
            ]
            return customer

    def upsert_customer(self, phone: str, **fields: Any) -> None:
        """Create or update. Blank incoming values never overwrite something already known.

        An agent that re-saves a customer having only asked for a name must not wipe the
        address collected in a call last week.
        """
        now = _now()
        known = self.find_customer(phone)
        merged = {
            k: (fields.get(k) or (known or {}).get(k) or "")
            for k in ("name", "email", "address", "area", "property_type")
        }
        with self.connect(write=True) as conn:
            if known:
                conn.execute(
                    "UPDATE customers SET name=?, email=?, address=?, area=?, "
                    "property_type=?, updated_at=? WHERE phone=?",
                    (*merged.values(), now, known["phone"]),
                )
            else:
                conn.execute(
                    "INSERT INTO customers (phone, phone_key, name, email, address, area,"
                    " property_type, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (phone, phone_key(phone), *merged.values(), now, now),
                )

    def add_job(self, phone: str, job: Any) -> None:
        """Work already done for this customer. Read when somebody claims a warranty, so
        it has to outlive the ticket that created it."""
        data = job if isinstance(job, dict) else asdict(job)
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs (job_id, phone, service_name, address,"
                " completed_at, technician_id, status) VALUES (?,?,?,?,?,?,?)",
                (
                    data["job_id"], phone, data.get("what", ""), data.get("address", ""),
                    data.get("finished_on") or None, data.get("technician") or None,
                    data.get("status", "completed"),
                ),
            )

    # ---- tickets -----------------------------------------------------
    def save_ticket(self, ticket: Any) -> None:
        """`Ticket` is `id`/`phone` here and was `ticket_id`/`customer_phone` in the
        generation this file came from. The columns kept the old names; only the reading
        changed, because renaming columns buys nothing and a migration can go wrong."""
        data = ticket if isinstance(ticket, dict) else asdict(ticket)
        now = _now()
        phone = data.get("phone", "")
        with self.connect(write=True) as conn:
            existing = conn.execute(
                "SELECT created_at FROM tickets WHERE ticket_id = ?", (data["id"],)
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO tickets (ticket_id, phone, phone_key, status,"
                " tags, history, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    data["id"], phone, phone_key(phone),
                    data.get("status", "New Inquiry"),
                    json.dumps(data.get("tags") or {}, default=str),
                    json.dumps(data.get("history") or [], default=str),
                    existing["created_at"] if existing else now, now,
                ),
            )

    def open_tickets(self, phone: str, closed: tuple[str, ...] = ("Closed",)) -> list[dict[str, Any]]:
        key = phone_key(phone)
        if not key:
            return []
        placeholders = ",".join("?" * len(closed))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tickets WHERE phone_key = ? "
                f"AND status NOT IN ({placeholders}) ORDER BY updated_at DESC",
                (key, *closed),
            ).fetchall()
        return [_decode_ticket(r) for r in rows]

    # ---- appointments ------------------------------------------------
    def save_appointment(self, appointment: Any, *, calendar_event_id: str = "") -> None:
        """`calendar_event_id` is the id Google gave the event, and it is the reason this
        method takes a keyword nothing else does: without somewhere to keep it, a booking
        can be made in the real calendar and never moved or cancelled again.

        Passing it blank on a later save keeps whatever is already stored, so writing a
        status change does not throw the link away.
        """
        data = appointment if isinstance(appointment, dict) else asdict(appointment)
        now = _now()
        start = data["starts"]
        with self.connect(write=True) as conn:
            existing = conn.execute(
                "SELECT created_at, calendar_event_id FROM appointments WHERE appointment_id = ?",
                (data["id"],),
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO appointments (appointment_id, kind, ticket_id, phone,"
                " phone_key, technician_id, start_at, duration_minutes, address, description,"
                " status, calendar_event_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    data["id"], data.get("kind", "standard"),
                    data.get("ticket_id", ""), data.get("phone", ""),
                    phone_key(data.get("phone", "")),
                    data.get("technician") or None,
                    start.isoformat() if isinstance(start, datetime) else str(start),
                    int(data.get("minutes") or 0), data.get("address", ""),
                    data.get("what", ""), data.get("status", "booked"),
                    calendar_event_id or (existing["calendar_event_id"] if existing else None),
                    existing["created_at"] if existing else now, now,
                ),
            )

    def appointments_for(self, phone: str) -> list[dict[str, Any]]:
        """Everything booked for this number, whichever ticket made it.

        Somebody ringing to move a visit booked last week is on a new ticket in a new
        process, and the number is the only thing that connects the two.
        """
        key = phone_key(phone)
        if not key:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE status = 'booked' AND phone_key = ? "
                "ORDER BY start_at",
                (key,),
            ).fetchall()
        return [dict(r) for r in rows]

    def calendar_event_id(self, appointment_id: str) -> str:
        """What Google called it, so a reschedule or a cancellation can reach the event."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT calendar_event_id FROM appointments WHERE appointment_id = ?",
                (appointment_id,),
            ).fetchone()
        return str(row["calendar_event_id"] or "") if row else ""

    def appointments_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Booked appointments in a window — what a slot search must not double-book over."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE status = 'booked' AND start_at >= ? "
                "AND start_at < ? ORDER BY start_at",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- follow-ups --------------------------------------------------
    def schedule_followup(
        self, *, ticket_id: str, kind: str, due_at: datetime, chat_id: str = "",
        summary: str = "",
    ) -> str:
        """Owe somebody a question later. Returns the id."""
        followup_id = self.next_id("FU")
        now = _now()
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO followups (followup_id, ticket_id, kind, chat_id, summary,"
                " due_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (followup_id, ticket_id, kind, chat_id, summary,
                 due_at.isoformat(), now, now),
            )
        return followup_id

    def due_followups(self, now: datetime) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM followups WHERE status IN ('scheduled','asked') "
                "AND due_at <= ? ORDER BY due_at",
                (now.isoformat(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_followup(self, followup_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.connect(write=True) as conn:
            conn.execute(
                f"UPDATE followups SET {sets}, updated_at = ? WHERE followup_id = ?",
                (*fields.values(), _now(), followup_id),
            )

    def open_followup(self, chat_id: str) -> dict[str, Any] | None:
        """The question this technician has been asked and not answered."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM followups WHERE chat_id = ? AND status = 'asked' "
                "ORDER BY updated_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    # ---- messages and events -----------------------------------------
    # ---- web chat sessions -------------------------------------------
    def open_chat_session(self, session_id: str, phone: str) -> None:
        """Idempotent: reopening the same id with the same number is not an error."""
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chat_sessions (session_id, phone, phone_key,"
                " asserted, created_at) VALUES (?, ?, ?, 'typed', ?)",
                (session_id, phone, phone_key(phone), _now()),
            )

    def chat_session_phone(self, session_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT phone FROM chat_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return str(row["phone"]) if row else ""

    def add_message(
        self, *, channel: str, speaker: str, text: str,
        phone: str = "", ticket_id: str = "", session_id: str = "",
    ) -> None:
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO messages (phone, phone_key, ticket_id, session_id, channel,"
                " speaker, text, at) VALUES (?,?,?,?,?,?,?,?)",
                (phone, phone_key(phone), ticket_id, session_id, channel, speaker, text, _now()),
            )

    def conversation(self, *, phone: str = "", session_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """Past messages for this customer, across every channel they have used."""
        with self.connect() as conn:
            if phone and phone_key(phone):
                rows = conn.execute(
                    "SELECT * FROM messages WHERE phone_key = ? ORDER BY at DESC LIMIT ?",
                    (phone_key(phone), limit),
                ).fetchall()
            elif session_id:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY at DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                return []
        return [dict(r) for r in reversed(rows)]

    def add_event(self, kind: str, *, ticket_id: str = "", detail: str = "", **payload: Any) -> None:
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO events (ticket_id, kind, detail, payload, at) VALUES (?,?,?,?,?)",
                (ticket_id, kind, detail, json.dumps(payload, default=str), _now()),
            )

    def events(self, ticket_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE ticket_id = ? ORDER BY id", (ticket_id,)
            ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"] or "{}")} for r in rows]

    # ---- conversations in flight -------------------------------------
    def save_conversation(self, session_id: str, state: dict[str, Any], *,
                          phone: str = "", project: str = "", node: str = "",
                          finished: bool = False) -> None:
        """One `Conversation.save()`, written after every turn.

        The columns beside `state` are all derivable from it and are stored anyway: a
        person looking at a stuck conversation wants to know which step it is standing in
        without parsing a JSON blob, and "which conversations are open" has to be a query.
        """
        now = _now()
        with self.connect(write=True) as conn:
            existing = conn.execute(
                "SELECT created_at FROM conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO conversations (session_id, phone_key, project,"
                " node, finished, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, phone_key(phone), project, node, int(bool(finished)),
                 json.dumps(state, default=str),
                 existing["created_at"] if existing else now, now),
            )

    def load_conversation(self, session_id: str) -> dict[str, Any] | None:
        """What `Conversation.resume` needs, or None for somebody new."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state FROM conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
        return json.loads(row["state"]) if row else None

    # ---- what has already been done to the outside world --------------
    def ledger(self, session_id: str) -> dict[str, Any]:
        """The `world.done` for this conversation, as it stood when last written."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, result FROM ledger WHERE session_id = ?", (session_id,)
            ).fetchall()
        return {r["key"]: json.loads(r["result"]) for r in rows}

    def note_intent(self, session_id: str, key: str, result: Any) -> None:
        """One entry, written twice: once before the call and once with the answer.

        Separate from `save_conversation` on purpose. The conversation is saved at the end
        of a turn, and the whole point of this row is to exist *during* one — between
        Twilio acknowledging a message and this process learning that it did.
        """
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ledger (session_id, key, result, at)"
                " VALUES (?,?,?,?)",
                (session_id, key, json.dumps(result, default=str), _now()),
            )

    def forget_intent(self, session_id: str, key: str) -> None:
        """It was refused, so it did not happen and must be allowed to be tried again."""
        with self.connect(write=True) as conn:
            conn.execute("DELETE FROM ledger WHERE session_id = ? AND key = ?",
                         (session_id, key))

    def unconfirmed(self, marker: str) -> list[dict[str, Any]]:
        """Everything attempted whose outcome nobody knows. A person reads this list."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ledger WHERE result = ? ORDER BY at",
                (json.dumps(marker),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- ids ---------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        """Sequential per prefix, and unique across restarts.

        The in-memory counter restarts at 1 every process, which is fine for a scenario and
        would hand two different customers the same ticket number in production.
        """
        table = {"TK": "tickets", "AP": "appointments",
                 "FU": "followups"}.get(prefix)
        if table is None:
            return f"{prefix}-{int(datetime.now().timestamp() * 1000) % 1_000_000:06d}"
        column = {"tickets": "ticket_id", "appointments": "appointment_id",
                  "followups": "followup_id"}[table]
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {column} FROM {table} WHERE {column} LIKE ? "
                f"ORDER BY length({column}) DESC, {column} DESC LIMIT 1",
                (f"{prefix}-%",),
            ).fetchone()
        highest = int(row[0].split("-")[-1]) if row else 0
        return f"{prefix}-{highest + 1:04d}"


def _decode_ticket(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["tags"] = json.loads(data.get("tags") or "{}")
    data["history"] = json.loads(data.get("history") or "[]")
    return data


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

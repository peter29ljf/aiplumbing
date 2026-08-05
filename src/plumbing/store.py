"""Durable storage for the things that must outlive a conversation.

Until now every conversation built a `World` from `config/world_seed.yaml` and threw it
away at the end. That is right for tests — each scenario wants a clean, known world — and
useless in production, where a returning customer must be found, a ticket must still be
open tomorrow, and an appointment must still be in the diary when the technician goes out.

So the store is **optional**. A `World` without one behaves exactly as it always has, which
is what the whole test suite depends on. A `World` with one loads from the database instead
of the seed, and writes through on every change.

Only what intake and small_job need is here: customers with their past jobs, tickets,
appointments, the message history, and an append-only event log. Payments, technician
call rounds and warranty reviews belong to the flows that are not going live yet, and a
table nobody writes to is a table that will be wrong by the time somebody does.

Three things are lifted from the sibling `aiphone` project, whose comments read like they
were paid for in production incidents:

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
        data = job if isinstance(job, dict) else asdict(job)
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs (job_id, phone, service_type, service_name,"
                " address, completed_at, technician_id, status, amount, warranty_excluded)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    data["job_id"], phone, data.get("service_type", ""),
                    data.get("service_name", ""), data.get("address", ""),
                    data.get("completed_at"), data.get("technician_id"),
                    data.get("status", "completed"), data.get("amount"),
                    int(bool(data.get("warranty_excluded"))),
                ),
            )

    # ---- tickets -----------------------------------------------------
    def save_ticket(self, ticket: Any) -> None:
        data = ticket if isinstance(ticket, dict) else asdict(ticket)
        now = _now()
        with self.connect(write=True) as conn:
            existing = conn.execute(
                "SELECT created_at FROM tickets WHERE ticket_id = ?", (data["ticket_id"],)
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO tickets (ticket_id, phone, phone_key, status,"
                " owner_agent, tags, history, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    data["ticket_id"], data.get("customer_phone", ""),
                    phone_key(data.get("customer_phone", "")),
                    data.get("status", "New Inquiry"), data.get("owner_agent", ""),
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
        data = appointment if isinstance(appointment, dict) else asdict(appointment)
        now = _now()
        start = data["start"]
        with self.connect(write=True) as conn:
            existing = conn.execute(
                "SELECT created_at, calendar_event_id FROM appointments WHERE appointment_id = ?",
                (data["appointment_id"],),
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO appointments (appointment_id, kind, ticket_id, phone,"
                " technician_id, start_at, duration_minutes, address, description, status,"
                " calendar_event_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    data["appointment_id"], data.get("kind", "standard"),
                    data.get("ticket_id", ""), data.get("customer_phone", ""),
                    data.get("technician_id"),
                    start.isoformat() if isinstance(start, datetime) else str(start),
                    int(data.get("duration_minutes") or 0), data.get("address", ""),
                    data.get("description", ""), data.get("status", "booked"),
                    calendar_event_id or (existing["calendar_event_id"] if existing else None),
                    existing["created_at"] if existing else now, now,
                ),
            )

    def appointments_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Booked appointments in a window — what a slot search must not double-book over."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE status = 'booked' AND start_at >= ? "
                "AND start_at < ? ORDER BY start_at",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- messages and events -----------------------------------------
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

    # ---- ids ---------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        """Sequential per prefix, and unique across restarts.

        The in-memory counter restarts at 1 every process, which is fine for a scenario and
        would hand two different customers the same ticket number in production.
        """
        table = {"TK": "tickets", "AP": "appointments"}.get(prefix)
        if table is None:
            return f"{prefix}-{int(datetime.now().timestamp() * 1000) % 1_000_000:06d}"
        column = {"tickets": "ticket_id", "appointments": "appointment_id"}[table]
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

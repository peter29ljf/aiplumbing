"""The world that reaches real people.

The seam is here and not in the tools, and that is the whole reason this file is short.
Every tool in `bat/presets/tools/service.py` that touches the outside world is a one-line
delegation:

    slots = world.free_slots()
    appointment = world.book(...)
    return world.send_sms(to, body)
    return world.notify_technician(technician_id, subject, body)
    return world.escalate(ticket_id, reason, details)

So a world that answers to those same names with Twilio and Google behind them needs no
tool to change, no `flow.yaml` to change, and no rule to be rewritten. Seventeen nodes that
have never been told which world they are standing in go on not knowing.

**Live is an overlay, never a replacement.** Every method calls `super()`, so the record
in `world.texts` is written whether or not anything left the machine, and every assertion
in every scenario reads the same in both modes. What differs is a `live: True` on the
record and the fact that somebody's phone went.

**Order is not a detail.** For anything that reaches out, the outward act comes first and
the bookkeeping second: a stop between the two leaves a record saying nothing happened when
something did, which is recoverable, rather than a record saying something happened when it
did not, which is a customer told a technician is coming. `book` is the exception and for
the same reason — the internal appointment exists first so the real calendar write can be
rolled back, because an agent must never be able to say "you're booked" over an entry no
technician can see.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from bat.live.integrations import LiveToolUnavailable, is_live
from bat.runtime.registry import UNCONFIRMED
from bat.runtime.sim import World
from bat.runtime.world import Refused

# What each of this world's outward acts is called at the switch.
#
# `is_live` is a string lookup and a name it does not recognise is not an error — it is
# silently, permanently mocked. An earlier tree registered `calendar.create` while the gate
# knew `calendar.create_appointment`, and nothing anywhere would ever have said so.
# `tests/test_gate_vocabulary.py` is what makes that impossible now.
SWITCHES = {
    "free_slots": "calendar.find_slots",
    "book": "calendar.create_appointment",
    "send_sms": "sms.send",
    "send_email": "email.send",
    "notify_technician": "telegram.send",
    "escalate": "telegram.send",
}

# How far either side of a candidate hour to ask the calendar about. Wide enough to catch a
# long job that started before the hour, narrow enough to be one request.
LOOKS_AROUND = timedelta(hours=8)


class Ledger:
    """`world.done`, kept in a database instead of in memory.

    `registry.call` writes the key before the handler runs and the answer after it, so that
    a process which dies between Twilio's acknowledgement and the answer comes back knowing
    something was attempted. That only means anything if the writing survives the death,
    and a dict does not.

    A dict is what `registry.call` thinks it has, so this is one — `in`, `[]`, `[]=`, `pop`
    and nothing else, because nothing else is used. Keeping the registry ignorant is what
    keeps the scenario path and the live path the same code.
    """

    def __init__(self, store: Any, session_id: str) -> None:
        self.store, self.session_id = store, session_id
        self._cache: dict[str, Any] = store.ledger(session_id)

    def __contains__(self, key: str) -> bool:
        return key in self._cache

    def __getitem__(self, key: str) -> Any:
        return self._cache[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._cache[key] = value
        self.store.note_intent(self.session_id, key, value)

    def __iter__(self):
        return iter(self._cache)

    def __len__(self) -> int:
        return len(self._cache)

    def pop(self, key: str, default: Any = None) -> Any:
        self.store.forget_intent(self.session_id, key)
        return self._cache.pop(key, default)

    def items(self):
        return self._cache.items()


class LiveWorld(World):
    """The simulated world, with the outward acts wired to real services.

    Everything not overridden below is the simulated behaviour, deliberately: the clock,
    the rules, the tickets, the tags. Only what leaves the machine is different.
    """

    def __init__(self, now: str, *, rules: dict[str, Any], store: Any,
                 session_id: str, seed: dict[str, Any] | None = None,
                 records: tuple[str, ...] = (), supervisor: str = "") -> None:
        super().__init__(now=now, seed=seed, rules=rules, records=records, store=store)
        self.session_id = session_id
        # Where an escalation goes when the ticket has no technician of its own. Blank
        # means nowhere, and `escalate` says so rather than pretending.
        self.supervisor = supervisor
        self.done = Ledger(store, session_id)   # type: ignore[assignment]

    @classmethod
    def restore(cls, state: dict[str, Any], *, rules: dict[str, Any], store: Any,
                session_id: str = "", records: tuple[str, ...] = (),
                supervisor: str = "") -> "LiveWorld":
        """A conversation coming back, into a world that still reaches real people.

        The base classmethod builds a plain simulated world whatever it is restoring, and
        one restored that way looks entirely correct: the customer is there, the ticket is
        there, the times are right. Its next booking goes into memory that is thrown away.
        """
        return cls(now=state["now"], rules=rules, store=store, session_id=session_id,
                   records=records, supervisor=supervisor)._reload(state)

    # ---- the diary ---------------------------------------------------
    def free_slots(self, *, days: int = 7, limit: int = 3) -> list[datetime]:
        """The real calendar's busy periods, folded into the ones already known.

        Read once per search rather than per candidate hour: the simulated `_taken` is
        called for every hour in the window, and a Google request inside that loop would be
        a hundred round trips to answer one question.
        """
        if is_live(SWITCHES["free_slots"]):
            from bat.live.integrations import google_calendar

            try:
                busy = google_calendar.busy_periods(
                    self.now - LOOKS_AROUND, self.now + timedelta(days=days) + LOOKS_AROUND)
            except LiveToolUnavailable as down:
                # Not an empty diary. An empty diary is a specific claim — "all of this is
                # free" — and offering a time on the strength of a failed lookup is how a
                # technician ends up double-booked.
                raise Refused(
                    f"The calendar could not be read, so I cannot say what is free: "
                    f"{down}. Do not offer a time. Use escalate.raise instead."
                ) from down
            known = set(self.busy)
            self.busy.extend(period for period in busy if period not in known)
        return super().free_slots(days=days, limit=limit)

    def book(self, **fields: Any):
        """Ours first, then Google's, and ours is undone if Google's fails.

        The other order looks tidier and is wrong: the internal booking is what the gates
        and the ledger act on, and a real calendar entry with nothing behind it is invisible
        to every check this system has.
        """
        appointment = super().book(**fields)
        if not is_live(SWITCHES["book"]):
            return appointment

        from bat.live.integrations import google_calendar

        try:
            event_id = google_calendar.create_event(
                start=appointment.starts,
                duration_minutes=appointment.minutes,
                summary=f"{appointment.what or 'Visit'} — {appointment.phone}".strip(" —"),
                description=f"Ticket {appointment.ticket_id}. "
                            f"Technician {appointment.technician or 'unassigned'}.",
                location=appointment.address,
            )
        except LiveToolUnavailable as down:
            self.appointments.pop(appointment.id, None)
            if self.store is not None:
                self.store.save_appointment(
                    {**vars(appointment), "status": "cancelled"})
            raise Refused(
                f"The calendar would not take that booking, so it is NOT booked: {down}. "
                f"Do not tell the customer they have a slot. Use escalate.raise."
            ) from down

        # The id Google gave it. Without this stored, the visit can never be moved or
        # cancelled again — the event exists and nothing here knows what it is called.
        if self.store is not None:
            self.store.save_appointment(appointment, calendar_event_id=event_id)
        return appointment

    # ---- telling people ----------------------------------------------
    def send_sms(self, to: str, body: str) -> dict[str, Any]:
        if not is_live(SWITCHES["send_sms"]):
            return super().send_sms(to, body)

        from bat.live.integrations import twilio_sms

        sent = self._out(twilio_sms.send_sms, "text", to, body)
        answer = super().send_sms(to, body)
        self._mark(self.texts, sent)
        return {**answer, "live": True, "message_id": sent.get("message_id", "")}

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        if not is_live(SWITCHES["send_email"]):
            return super().send_email(to, subject, body)

        from bat.live.integrations import gmail_email

        sent = self._out(gmail_email.send_email, "email", to, subject, body)
        answer = super().send_email(to, subject, body)
        self._mark(self.emails, sent)
        return {**answer, "live": True, "message_id": sent.get("message_id", "")}

    def notify_technician(self, technician_id: str, subject: str,
                          body: str) -> dict[str, Any]:
        technician = self.technicians.get(technician_id)
        if technician is None:
            # Before anything is sent, and with the same words the simulated world uses.
            raise Refused(
                f"No technician '{technician_id}'. On duty: {sorted(self.technicians)}")
        if not is_live(SWITCHES["notify_technician"]):
            return super().notify_technician(technician_id, subject, body)

        from bat.live.integrations import telegram

        chat_id = technician.telegram
        if not chat_id:
            raise Refused(
                f"{technician.name} has no Telegram id on the roster, so there is no way "
                f"to reach them. Use escalate.raise."
            )
        sent = self._out(telegram.send_message, "message to the technician",
                         chat_id, f"{subject}\n\n{body}")
        answer = super().notify_technician(technician_id, subject, body)
        self._mark(self.technician_messages, sent)
        return {**answer, "live": True}

    def escalate(self, ticket_id: str, reason: str, details: str) -> dict[str, Any]:
        """A person is being asked to pick this up, so a person has to actually hear it.

        The simulated world appends to a list a scenario reads. Here the list is still
        written — every assertion depends on it — and the message also goes out, because an
        escalation nobody receives is the failure this whole flow exists to avoid.
        """
        answer = super().escalate(ticket_id, reason, details)
        if not is_live(SWITCHES["escalate"]):
            return answer

        from bat.live.integrations import telegram

        where = self.supervisor or next(
            (t.telegram for t in self.technicians.values() if t.telegram), "")
        if not where:
            raise Refused(
                "There is nobody to escalate to: no supervisor chat is configured and no "
                "technician on the roster has a Telegram id. Tell the customer somebody "
                "will call them back, and say nothing about timing."
            )
        ticket = self.ticket(ticket_id)
        sent = self._out(
            telegram.send_message, "escalation", where,
            f"{reason} — {ticket.id}\n\n{details}\n\n"
            f"{json.dumps(ticket.tags, indent=2, default=str)}",
        )
        self._mark(self.escalations, sent)
        return {**answer, "live": True}

    # ---- follow-ups ---------------------------------------------------
    def schedule_followup(self, ticket_id: str, hours: int) -> dict[str, Any]:
        """Nothing leaves the machine here, so there is no switch — but it has to be
        durable, which is the same reason in a different form. The conversation that
        arranges a follow-up ends; a follow-up held only in that conversation's memory
        fires exactly never, and that is what the first generation shipped."""
        answer = super().schedule_followup(ticket_id, hours)
        if self.store is not None:
            self.store.schedule_followup(
                ticket_id=ticket_id, kind="job_outcome",
                due_at=self.now + timedelta(hours=int(hours)),
                chat_id=next((t.telegram for t in self.technicians.values()
                              if t.telegram), ""),
            )
        return answer

    # ------------------------------------------------------------------
    def _out(self, send: Any, what: str, *args: Any) -> dict[str, Any]:
        """Make the outward call, and turn any failure into "it did not happen".

        Every adapter raises `LiveToolUnavailable` for all of it — unreachable, rejected,
        credentials missing, library missing — so there is one thing to catch. What the
        step is told has to say plainly that nothing was sent, or it writes a message to
        the customer on the strength of a call that failed.
        """
        try:
            return send(*args)
        except LiveToolUnavailable as down:
            raise Refused(f"The {what} did not send: {down}. Nothing has gone out — do "
                          f"not tell anyone it has.") from down

    @staticmethod
    def _mark(records: list[dict[str, Any]], sent: dict[str, Any]) -> None:
        """Stamp the record `super()` just wrote as one that really left the machine.

        The provider's own id goes on it, which is the only way to go and look at a
        specific message afterwards when somebody says they never got it.
        """
        if records:
            records[-1].update({"live": True,
                                "provider": sent.get("provider", ""),
                                "provider_message_id": sent.get("message_id", "")})

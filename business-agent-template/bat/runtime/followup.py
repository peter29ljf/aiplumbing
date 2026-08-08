"""The day after, and the day after that, until somebody answers.

Five of the seven ways a conversation ends leave work with a person: a visit booked, a
claim to judge, a project to price, somebody who needs help now, an appointment to move.
The conversation is over at that point and the ticket is not. Something has to ask the
technician whether it got done, and close it when it did.

**It does not give up.** The old one did — `src/plumbing/live/reminders.py` stops after two
asks and marks the followup `given_up`, on the reasoning that a bot which keeps asking gets
muted. That reasoning is about the bot. From the customer's side an unanswered question is
not a finished job, and a ticket nobody closed is one nobody looks at again. So this asks
every day, forever, and the only thing that stops it is an answer.

No model calls anywhere in here. Asking a technician whether a job is done, and thanking a
customer when it is, contain no judgement worth spending a model on — and a loop that runs
unattended for days is the last place to want one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bat.runtime.sim import World

# Both a day. Separate names because they are separate decisions: how long to leave the
# technician alone before the first ask, and how long between asks after that.
FIRST_ASK_HOURS = 24
ASK_AGAIN_HOURS = 24

THANKS = (
    "Thanks for choosing {company}. {technician} has confirmed the work is complete. "
    "If anything isn't right, reply to this message and we'll come back out."
)


def due(world: World, now: datetime | None = None) -> list[dict[str, Any]]:
    """Follow-ups waiting to be asked. Answered ones are done with."""
    when = now or world.now
    return [f for f in world.followups
            if not f.get("answered") and datetime.fromisoformat(f["due"]) <= when]


def tick(world: World, now: datetime | None = None) -> list[dict[str, Any]]:
    """Ask about everything that has come due, and set the next ask a day out.

    Returns what was asked, so a caller can tell "nothing was due" from "nobody to ask".
    """
    when = now or world.now
    asked = []
    for followup in due(world, when):
        technician = _who(world, followup)
        if technician is None:
            continue                      # nobody on duty; ask again tomorrow regardless

        # The outward thing first, the bookkeeping after — and never the other way round.
        #
        # It was the other way round: the due time moved to tomorrow and *then* the
        # message went out. A stop between the two — the process killed, the messaging
        # service down — lost that day's ask entirely and rearmed for tomorrow, so the job
        # sat unchased while the record said it had been chased. Of the two ways to get
        # this wrong, that is the worse one: asking twice annoys a technician, and not
        # asking leaves a customer waiting on a job nobody is watching.
        #
        # Nothing here is a retry loop. If the send raises, the follow-up is untouched and
        # still due, so the next tick asks again. That is the whole recovery.
        world.technician_messages.append({
            "technician_id": technician.id,
            "subject": f"Follow-up: {followup['ticket_id']}",
            # Read before `asked` moves, so the count in the message is the count of this
            # ask rather than of the next one.
            "body": _ask(world, followup),
            "channel": "telegram",
            "kind": "followup",
            "at": when.isoformat(),
        })
        followup["asked"] = int(followup.get("asked", 0)) + 1
        followup["due"] = (when + timedelta(hours=ASK_AGAIN_HOURS)).isoformat()
        asked.append(followup)
    return asked


def technician_says(world: World, ticket_id: str, *, done: bool,
                    note: str = "", now: datetime | None = None) -> dict[str, Any]:
    """His answer. Done closes the ticket and thanks the customer; anything else waits."""
    when = now or world.now
    followup = next((f for f in world.followups if f["ticket_id"] == ticket_id
                     and not f.get("answered")), None)
    if followup is None:
        return {"ok": False, "error": f"No open follow-up for {ticket_id}"}

    followup["note"] = note
    if not done:
        # Still going, so still ours to chase. The ask count is not reset: how many times
        # somebody has been chased about one job is the thing worth being able to see.
        followup["due"] = (when + timedelta(hours=ASK_AGAIN_HOURS)).isoformat()
        return {"answered": False, "asked": followup.get("asked", 0)}

    followup["answered"] = True
    world.set_status(ticket_id, "Closed")
    _thank(world, ticket_id, followup, when)
    return {"answered": True, "closed": True, "asked": followup.get("asked", 0)}


# ----------------------------------------------------------------------
def _who(world: World, followup: dict[str, Any]):
    """The technician this job belongs to, or whoever is on duty."""
    appointment = next((a for a in world.appointments.values()
                        if a.ticket_id == followup["ticket_id"]), None)
    if appointment and appointment.technician in world.technicians:
        return world.technicians[appointment.technician]
    return next(iter(world.technicians.values()), None)


def _ask(world: World, followup: dict[str, Any]) -> str:
    ticket = world.tickets.get(followup["ticket_id"])
    what = (ticket.tags.get("issue") if ticket else "") or "the job"
    # `asked` is still the count *before* this ask, because the message is written
    # before the bookkeeping moves. This is that ask's own number.
    times = int(followup.get("asked", 0)) + 1
    chased = f" (asked {times} times now)" if times > 1 else ""
    return (f"Is {followup['ticket_id']} — {what} — finished?{chased} "
            f"Reply done, or tell me what is still outstanding.")


def _thank(world: World, ticket_id: str, followup: dict[str, Any],
           when: datetime) -> None:
    ticket = world.tickets.get(ticket_id)
    if ticket is None:
        return
    # `ticket.phone` is only set when the model happened to pass "phone" to set_fields.
    # The number that is reliably there is the one the engine copied from the lookup's
    # `remembers` — reading only the first meant a job closed with no thank-you at all.
    number = ticket.phone or str(ticket.tags.get("phone") or "")
    if not number:
        return                            # nobody to text; the close still stands
    technician = _who(world, followup)
    world.texts.append({
        "to": number,
        "body": THANKS.format(
            company=world.rules["company"]["name"],
            technician=technician.name if technician else "The technician",
        ),
        "kind": "thanks",
        "at": when.isoformat(),
    })

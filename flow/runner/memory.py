"""What the next node is told about everything that came before it.

Not the transcript. A node is given a handful of facts the conversation has settled —
who this is, what is wrong, which building, what they chose — and works from those. The
words that produced them are gone.

The facts live on the ticket, in `tags`, written by `ticket.set_fields`. There is no
second store and no session file: a fact in two places is a fact that will disagree with
itself, and this project has spent a day on that already.

Everything here is a view over `world.tickets[...].tags`.
"""

from __future__ import annotations

from typing import Any

# Where the conversation is, kept on the ticket like everything else. Named rather than
# spelled out at each use: it is written by the engine and skipped by `summarise` below,
# which is two places that have to agree, and a typo in either would be invisible.
NODE_TAG = "flow_node"

# The ones worth carrying between nodes, in the order a person would want to read them.
# Anything else on the ticket stays there and is read by whoever needs it — this is the
# summary, not the record.
CARRIED = (
    ("customer_name", "Name"),
    ("phone", "Phone"),
    ("address", "Address"),
    ("email", "Email"),
    ("property_type", "Property"),
    # "Problem" once, and it framed everything as a fault waiting to be fixed — a ticket
    # reading "Problem: wants Friday's appointment moved to next week" pulled `identify`
    # onto the ordinary repair path in half the runs, while the same node handed the same
    # words straight to `booking_change` when the customer said them out loud. What the
    # label calls it is what the branch believes it is.
    ("issue", "They came about"),
    ("risk", "Risk"),
    ("size", "Job size"),
    ("warranty_status", "Warranty"),
    ("service_choice", "They chose"),
)


def summarise(tags: dict[str, Any], *, ticket_id: str = "") -> str:
    """The one paragraph a node sees instead of the history.

    Says what is *not* known as plainly as what is. A node that cannot tell the difference
    between "no email on file" and "nobody has asked yet" asks again, and being asked
    twice for the same thing is the clearest sign nobody is listening.
    """
    lines = [f"Ticket: {ticket_id}"] if ticket_id else []
    for key, label in CARRIED:
        value = tags.get(key)
        if value not in (None, "", []):
            lines.append(f"{label}: {value}")

    # Everything else that has been written down. The named ones above are only there to
    # be read in a sensible order — a whitelist alone means a step records something and
    # the next step cannot see it, which is how a node came to offer appointment times and
    # the one that books them said it did not have any.
    for key, value in tags.items():
        if key in (NODE_TAG, "outcome") or any(key == k for k, _ in CARRIED):
            continue
        if value not in (None, "", [], {}):
            lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")

    missing = [label for key, label in CARRIED if not tags.get(key)]
    if not lines or (ticket_id and len(lines) == 1):
        return "Nothing is known about this customer yet — this is the start."

    if missing:
        lines.append("Not established yet: " + ", ".join(missing))
    return "\n".join(lines)


def node_of(tags: dict[str, Any], default: str) -> str:
    return str(tags.get(NODE_TAG) or default)

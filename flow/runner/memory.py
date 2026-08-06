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

# The ones worth carrying between nodes, in the order a person would want to read them.
# Anything else on the ticket stays there and is read by whoever needs it — this is the
# summary, not the record.
CARRIED = (
    ("customer_name", "Name"),
    ("phone", "Phone"),
    ("address", "Address"),
    ("email", "Email"),
    ("property_type", "Property"),
    ("issue", "Problem"),
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

    missing = [label for key, label in CARRIED if not tags.get(key)]
    if not lines or (ticket_id and len(lines) == 1):
        return "Nothing is known about this customer yet — this is the start."

    if missing:
        lines.append("Not established yet: " + ", ".join(missing))
    return "\n".join(lines)


def node_of(tags: dict[str, Any], default: str) -> str:
    return str(tags.get("flow_node") or default)


def remember_node(tags: dict[str, Any], node: str) -> None:
    tags["flow_node"] = node

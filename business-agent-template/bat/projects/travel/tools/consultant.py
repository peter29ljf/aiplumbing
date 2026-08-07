"""The handover for Pacific Compass Travel — sending the enquiry to the consultant, and
the completeness gate that guards it."""

from __future__ import annotations

from bat.runtime.registry import Refused, _ticket, tool
from bat.runtime.world import AnyWorld


def _get(ticket, key, default=None):
    if ticket is None:
        return default
    if hasattr(ticket, "get"):
        return ticket.get(key, default)
    if isinstance(ticket, dict):
        return ticket.get(key, default)
    return default


@tool(
    "consultant.send_enquiry",
    "Sends the enquiry to the consultant already named on the ticket (decided by "
    "rules.check_destination). Refuses to send if any of the five must-haves is "
    "missing: party (adults and each child's age), route (origin, destination, "
    "fixed-or-flexible), budget (amount and per-person-or-total), passports (issuing "
    "country), scope (flights only, flights+hotel, or full package). Returns the "
    "confirmation plus the deposit and insurance sentences so the reply can carry "
    "them without a second lookup.",
    {"ticket_id": {"type": "string"}},
    remembers=("sent_to",),
)
def consultant_send_enquiry(world: AnyWorld, ticket_id: str) -> dict:
    ticket = _ticket(world, ticket_id)
    missing = [
        f for f in ("party", "route", "budget", "passports", "scope")
        if not _get(ticket, f)
    ]
    if missing:
        raise Refused(
            "This enquiry is not ready to send — it is missing: "
            + ", ".join(missing)
            + ". Finish collecting those before handing over."
        )
    consultant = _get(ticket, "consultant", None) or "the enquiry rota"
    return {
        "ok": True,
        "sent": True,
        "to": consultant,
        "confirmation": (
            f"Your enquiry is on its way to {consultant}, who will come back with "
            "options and a price."
        ),
        "deposit_sentence": (
            "When you book, the deposit is 20% and your consultant takes it — never "
            "anything in this chat."
        ),
        "insurance_sentence": (
            "And travel insurance is always quoted separately, alongside the trip."
        ),
    }
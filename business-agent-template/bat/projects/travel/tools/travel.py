"""Client tools for Pacific Compass Travel — identifying who is asking and how the
consultant reaches them."""

from __future__ import annotations

from bat.runtime.registry import Refused, tool
from bat.runtime.world import AnyWorld


@tool(
    "client.lookup",
    "Looks a client up by phone or email and returns whether we have travelled them "
    "before, so the reply can be warmer and skip re-asking anything we already hold. "
    "Takes either a phone or an email; at least one is needed.",
    {"phone": {"type": "string"}, "email": {"type": "string"}},
)
def client_lookup(world: AnyWorld, phone: str = None, email: str = None) -> dict:
    contact = phone or email
    if not contact:
        raise Refused("I need a phone number or an email before I can look you up.")
    # No record store in this build — every enquiry is a fresh sheet, exactly as the
    # plan assumes. looked-up-ness changes nothing.
    return {"found": False, "contact": contact, "known_customer": "no"}


@tool(
    "client.save",
    "Records the client's name and how the consultant reaches them (phone or email). "
    "Writes these to the ticket. Call it once you have their name and at least one "
    "contact detail.",
    {"name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"},
     "language": {"type": "string"}},
    remembers=("name", "phone", "email", "language"),
)
def client_save(world: AnyWorld, name: str = None, phone: str = None,
                email: str = None, language: str = None) -> dict:
    if not name:
        raise Refused("No name was given to save.")
    return {"ok": True, "saved": True}
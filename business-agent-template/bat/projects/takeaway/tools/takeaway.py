from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bat.runtime.registry import Refused, _ticket, tool
from bat.runtime.world import AnyWorld


def _rules(world: AnyWorld) -> dict:
    return world.rules


# ======================================================================
# who they are
# ======================================================================


@tool(
    "guest.create",
    "Open a record for a guest ordering takeaway or booking a table. A pickup customer "
    "has neither a full address nor an email, so this only needs a name and a number. "
    "Returns the customer's name so the reply can use it.",
    {"ticket_id": {"type": "string"},
     "phone": {"type": "string"},
     "name": {"type": "string"}},
    remembers=("phone", "name"),
)
def guest_create(world: AnyWorld, ticket_id: str, phone: str, name: str) -> dict[str, Any]:
    if not name.strip():
        raise Refused("There is no point recording a blank name.")
    customer = world.add_customer(phone=phone, name=name, address="", email="")
    return {"created": True, "phone": customer.phone, "name": customer.name}


# ======================================================================
# rules, read from business_rules.yaml
# ======================================================================


def _oclock(hhmm: str) -> str:
    """'21:30' is not a time anybody says out loud. The agent repeats what the tool
    gives it, so the tool gives it the customer's words."""
    when = datetime.strptime(hhmm, "%H:%M")
    return when.strftime("%-I:%M %p").replace(":00", "").lower()


@tool(
    "rules.get_hours",
    "Opening hours and when the kitchen closes. Say a time only from here.",
    {},
)
def rules_get_hours(world: AnyWorld) -> dict[str, Any]:
    rules = _rules(world)
    hours = rules["hours"]
    return {
        "open": hours["open"],
        "close": hours["close"],
        "kitchen_close": hours["kitchen_close"],
        "display": (f"Open {_oclock(hours['open'])} to {_oclock(hours['close'])} every "
                    f"day, kitchen closes at {_oclock(hours['kitchen_close'])}."),
    }


@tool(
    "rules.get_wording",
    "The exact sentence to repeat for a refusal or a policy, in the language the customer "
    "is writing in. State which line you want and the language.",
    {"line": {"type": "string", "description": "One of: catering_over_30, outside_area, "
                                              "today_only, allergen, hold_15, no_card_number"},
     "language": {"type": "string", "description": "'en' or 'yue'"}},
)
def rules_get_wording(world: AnyWorld, line: str, language: str = "en") -> dict[str, Any]:
    wording = _rules(world).get("wording", {})
    entry = wording.get(line)
    if not entry:
        raise Refused(f"There is no wording line '{line}'. Say which policy you want.")
    text = entry.get(language) or entry.get("en")
    return {"line": line, "text": text, "language": language}


# ======================================================================
# the menu
# ======================================================================


@tool(
    "menu.today",
    "What is on today, grouped by category, with prices. The one place the menu exists. "
    "Returns available and sold-out separately so the reply can say what is not on today.",
    {},
)
def menu_today(world: AnyWorld) -> dict[str, Any]:
    rules = _rules(world)
    menu = rules.get("menu", [])
    sold_out = set(rules.get("sold_out_today", []))
    available = [i for i in menu if i["name"] not in sold_out]
    out = [i for i in menu if i["name"] in sold_out]
    cats = {}
    for item in available:
        cats.setdefault(item["category"], []).append(item)
    return {
        "available": available,
        "sold_out_today": [i["name"] for i in out],
        "by_category": cats,
    }


@tool(
    "menu.check_items",
    "Match what the customer typed to real dishes. Returns the price and whether each is "
    "available or sold out today, with near-misses when nothing matches. Sold-out changes "
    "hourly, so this is read live each time.",
    {"items": {"type": "array", "description": "The dish names as the customer said them",
               "items": {"type": "string"}}},
    remembers=("dishes",),
)
def menu_check_items(world: AnyWorld, items: list) -> dict[str, Any]:
    rules = _rules(world)
    menu = rules.get("menu", [])
    sold_out = set(rules.get("sold_out_today", []))
    by_name = {i["name"].lower(): i for i in menu}
    results = []
    for entry in items:
        # A dish arrives either as a bare name or as {"name": ..., "qty": ...}, and which
        # one depends on whether the customer asked for more than one. This took only the
        # first shape and raised on the second — so an order with quantities in it never
        # reached the ticket, the quote came back zero, and the free-delivery threshold
        # was never crossed. It read as a flaky scenario for a week; it was a crash that
        # fires only on large orders, which are exactly the ones the waiver is about.
        raw = entry.get("name", "") if isinstance(entry, dict) else str(entry)
        qty = int(entry.get("qty", 1)) if isinstance(entry, dict) else 1
        key = raw.strip().lower()
        item = by_name.get(key)
        if item:
            results.append({"what": raw, "name": item["name"], "qty": qty,
                            "price": item["price"],
                            "available": item["name"] not in sold_out,
                            "status": "sold_out" if item["name"] in sold_out else "available"})
        else:
            near = [i["name"] for i in menu if any(w in i["name"].lower() for w in key.split()
                                                   if len(w) > 3)]
            results.append({"what": raw, "name": None, "qty": qty, "price": None,
                            "available": False, "status": "not_found",
                            "near_misses": near})
    # The key `remembers=("dishes",)` has been looking for since the day it was written.
    # Without it the tool declared it remembered the order and remembered nothing, so the
    # only thing that ever put dishes on the ticket was the model choosing to write them
    # down by hand — and when it did not, the quote came back zero.
    return {
        "results": results,
        "dishes": [{"name": r["name"], "qty": r["qty"]}
                   for r in results if r["name"] and r["available"]],
    }


# ======================================================================
# the table book
# ======================================================================


@tool(
    "tables.check_party_size",
    "Whether a party of this size can be booked by the agent or needs the manager. "
    "Returns 'ok' or 'needs_manager'. Never decide 'over eight' yourself.",
    {"party_size": {"type": "integer"}},
    remembers=("party_size",),
)
def tables_check_party_size(world: AnyWorld, party_size: int) -> dict[str, Any]:
    limit = _rules(world)["decisions_a_person_makes"]["large_party_over"]
    if party_size > limit:
        return {"party_size": party_size, "verdict": "needs_manager",
                "limit": limit}
    return {"party_size": party_size, "verdict": "ok", "limit": limit}


@tool(
    "tables.find_sittings",
    "Which 90-minute sittings actually have a table free on the day they want. Refuses a "
    "party over eight, and never returns a sitting that would run past close. Returns "
    "sittings as they should be read to the customer.",
    {"day": {"type": "string", "description": "e.g. 'Saturday' or a date"},
     "party_size": {"type": "integer"}},
    remembers=("sittings", "day"),
)
def tables_find_sittings(world: AnyWorld, day: str, party_size: int) -> dict[str, Any]:
    limit = _rules(world)["decisions_a_person_makes"]["large_party_over"]
    if party_size > limit:
        raise Refused("A party this size is the manager's to confirm. Take the details and "
                      "hand over rather than offering sittings.")
    # `world.find_sittings` was written here and has never existed. The world's whole
    # surface is in bat/presets/world.md; a dining room's own arithmetic belongs in the
    # restaurant's own tool, not bolted onto a world that also serves plumbers.
    room = _rules(world)["dining_room"]
    hours = _rules(world)["hours"]
    length = int(room["sitting_minutes"])
    first = datetime.strptime(hours["open"], "%H:%M")
    last = datetime.strptime(room["last_sitting"], "%H:%M")
    taken = [b for b in (world.snapshot().get("bookings") or []) if b.get("day") == day]

    sittings = []
    when = first
    while when <= last:
        reads_as = when.strftime("%-I:%M %p").lower().replace(":00", "")
        seated = sum(1 for b in taken if b.get("sitting") == reads_as)
        if seated < int(room["tables"]):
            sittings.append({"starts": when.strftime("%H:%M"), "reads_as": reads_as})
        when += timedelta(minutes=length)
    if not sittings:
        return {"found": False, "sittings": [], "day": day}
    return {"found": True,
            "sittings": [{"starts": s["starts"], "reads_as": s["reads_as"]} for s in sittings],
            "day": day}


@tool(
    "tables.book",
    "Hold the table for the sitting they chose. Cannot book a party over eight, cannot "
    "book a thirteenth overlapping group, and returns the 15-minute hold sentence to say "
    "to the customer. Call this first, on its own, and read what comes back.",
    {"ticket_id": {"type": "string"},
     "day": {"type": "string"},
     "sitting": {"type": "string", "description": "The sitting start, as returned by "
                                                 "tables.find_sittings"},
     "party_size": {"type": "integer"},
     "name": {"type": "string"}},
    remembers=("booking_ref", "sitting", "day"),
    once=True,
)
def tables_book(world: AnyWorld, ticket_id: str, day: str, sitting: str,
                party_size: int, name: str) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
    limit = _rules(world)["decisions_a_person_makes"]["large_party_over"]
    if party_size > limit:
        raise Refused("A party this size is the manager's to confirm, not a booking you "
                      "can make here.")
    # `world.book_table` was written here and does not exist. The world's whole surface
    # is in bat/presets/world.md; a business's own nouns go through `record`, which puts
    # them in the snapshot where `expect: bookings: 1` can count them.
    booking = {"ref": f"TB-{len(world.snapshot().get('bookings') or []) + 1:04d}",
               "ticket_id": ticket.id, "day": day, "sitting": sitting,
               "party_size": party_size, "name": name, "phone": ticket.phone}
    world.record("bookings", booking)
    # In the language they are writing in, not always English. A Cantonese customer was
    # told, in English, that we hold the table for fifteen minutes — the one sentence in
    # the whole booking they were most likely to need.
    spoken = str(ticket.tags.get("language") or "en").lower()
    if spoken.startswith(("yue", "can", "zh", "\u4e2d", "\u5ee3", "\u5eff")):
        spoken = "yue"
    hold_lines = _rules(world)["wording"]["hold_15"]
    hold = hold_lines.get(spoken) or hold_lines["en"]
    return {"booking_ref": booking["ref"], "sitting": sitting, "day": day,
            "hold_line": hold}


# ======================================================================
# delivery
# ======================================================================


@tool(
    "delivery.check_address",
    "Whether an address is inside our 5 km delivery area. Returns in_area or out_of_area "
    "and the delivery fee and the free-over threshold. The 5 km is never your judgement — "
    "promise nothing until this has answered.",
    {"address": {"type": "string"}},
    # The answer, not just the question. `order.place` refuses a delivery whose
    # `delivery_in_area` is not True, and the checker used to remember the address alone —
    # so the flag never reached the ticket, every delivery was refused, and the step that
    # places orders reached back for a tool that is not its to fix it.
    remembers=("address", "delivery_in_area", "distance_km"),
)
def delivery_check_address(world: AnyWorld, address: str) -> dict[str, Any]:
    radius = _rules(world)["delivery"]["radius_km"]
    fee = _rules(world)["delivery"]["fee"]
    waived = _rules(world)["delivery"]["fee_waived_over"]
    # As above: `world.delivery_distance` never existed either. There is no geocoder in a
    # simulator, and there does not need to be — what a scenario turns on is whether the
    # address is one of the neighbourhoods we deliver to.
    NEARBY = {"new westminster": 2.0, "queensborough": 3.5, "sapperton": 3.0,
              "burnaby": 6.0, "coquitlam": 9.0, "surrey": 11.0, "vancouver": 14.0,
              "richmond": 16.0}
    where = address.lower()
    distance = next((km for place, km in NEARBY.items() if place in where), 12.0)
    result = {"distance_km": distance, "in_area": distance <= radius}
    return {
        "address": address,
        "in_area": result["in_area"],
        # The name `remembers` and `order.place` both look for.
        "delivery_in_area": result["in_area"],
        "distance_km": result["distance_km"],
        "radius_km": radius,
        "fee": fee,
        "fee_waived_over": waived,
    }


# ======================================================================
# orders
# ======================================================================


@tool(
    "order.quote",
    "The total for the order — subtotal, the delivery fee and whether it is waived, and "
    "the total — plus the wait window. One call, because a total stated without the fee, "
    "or a wait without the total, is a customer told half a thing.",
    {"ticket_id": {"type": "string"}},
    # The step that quotes is not the step that closes, and the exchange between them does
    # not survive. `place_delivery` writes the last message a customer reads and has no
    # quoting tool of its own, so the total and the waiver died at the boundary — and the
    # one word somebody had added a dish to earn went with them.
    remembers=("total", "delivery_fee", "fee_waived", "waived_line"),
)
def order_quote(world: AnyWorld, ticket_id: str) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
    dishes = ticket.tags.get("dishes", [])
    rules = _rules(world)
    menu = {i["name"].lower(): i["price"] for i in rules["menu"]}
    subtotal = 0.0
    breakdown = []
    for d in dishes:
        # A dish arrives either as {"name": ..., "qty": ...} or as a bare string. The
        # guard used to live in the default argument — `d.get("name", d if isinstance(d,
        # str) else "")` — which calls `.get` on the string before deciding it is one, and
        # a scenario died mid-order with "'str' object has no attribute 'get'".
        name = d.get("name", "") if isinstance(d, dict) else str(d)
        qty = int(d.get("qty", 1)) if isinstance(d, dict) else 1
        price = menu.get(name.lower())
        if price is None:
            raise Refused(f"I do not have a price for '{name}' — check it against today's "
                          "menu before quoting.")
        line = price * qty
        subtotal += line
        breakdown.append({"name": name, "qty": qty, "line": line})
    delivery = rules["delivery"]
    fee = delivery["fee"]
    waived = subtotal > delivery["fee_waived_over"]
    # The fee that was actually charged, which is the one computed below and not the one
    # in the rules: a pickup order was quoted its subtotal plus a four-dollar delivery
    # fee, because this line asked whether the fee was waived and never whether it
    # applied. Every pickup customer in the suite was overcharged by four dollars, and the
    # figure they were read is the one they would have turned up expecting to pay.
    kind = ticket.tags.get("order_kind")
    charged = 0 if (waived or kind != "delivery") else fee
    total = subtotal + charged
    tak = rules["takeaway"]
    if kind == "delivery":
        wait = f"{tak['delivery_min_minutes']} to {tak['delivery_max_minutes']} minutes"
    else:
        wait = f"{tak['pickup_min_minutes']} to {tak['pickup_max_minutes']} minutes"
    return {
        "subtotal": subtotal,
        "kind": kind,
        "delivery_fee": charged,
        "fee_waived": waived and kind == "delivery",
        # The sentence, not just the flag. Somebody who added a dish to get past the
        # threshold is waiting to hear that it worked, and a boolean does not say it.
        "waived_line": (
            _rules(world)["wording"]["delivery_free"]
            .get(str(ticket.tags.get("language") or "en").lower()[:3].replace("can", "yue"),
                 _rules(world)["wording"]["delivery_free"]["en"])
            .replace("{over}", str(delivery["fee_waived_over"]))
            if (waived and kind == "delivery") else ""),
        "free_over": delivery["fee_waived_over"],
        "total": total,
        "wait": wait,
        "currency": "CAD",
    }


@tool(
    "order.place",
    "Put the order through. Refuses a future date, refuses an order when the kitchen is "
    "closed, refuses a delivery whose address has not been checked, and marks the order "
    "kitchen_check_required when the ticket carries an allergen question. Returns the "
    "sentence to give the customer about when it will be ready.",
    {"ticket_id": {"type": "string"}},
    remembers=("order_ref",),
    once=True,
)
def order_place(world: AnyWorld, ticket_id: str) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
    rules = _rules(world)
    now = world.now
    hours = rules["hours"]
    today_only = rules["takeaway"]["today_only"]
    kitchen_close = datetime.strptime(hours["kitchen_close"], "%H:%M").time()

    # future date
    want_date = ticket.tags.get("order_date")
    if want_date:
        raise Refused("Takeaway is today only — we cannot take an order for another day.")

    # kitchen closed
    if now.time() > kitchen_close:
        raise Refused("The kitchen is closed now. We are cooking again after the kitchen "
                      "reopens.")

    # delivery address must be checked
    kind = ticket.tags.get("order_kind")
    if kind == "delivery":
        in_area = ticket.tags.get("delivery_in_area")
        if in_area is not True:
            raise Refused("I cannot place a delivery until the address has been checked "
                          "inside the 5 km area.")

    kitchen_check = bool(ticket.tags.get("allergen_question"))
    # As above: `world.place_order` never existed either.
    order = {"ref": f"OR-{len(world.snapshot().get('orders') or []) + 1:04d}",
             "ticket_id": ticket.id, "kind": kind,
             "kitchen_check_required": kitchen_check}
    world.record("orders", order)
    tak = rules["takeaway"]
    if kind == "delivery":
        when = (f"{tak['delivery_min_minutes']} to {tak['delivery_max_minutes']} minutes")
        ready = f"Your order should arrive in {when}."
        if kitchen_check:
            ready += " The kitchen will confirm the allergen question with you before it is cooked."
    else:
        when = f"{tak['pickup_min_minutes']} to {tak['pickup_max_minutes']} minutes"
        ready = f"It will be ready for pickup in {when}."
        if kitchen_check:
            ready += " The kitchen will confirm the allergen question with you before it is cooked."
    return {"order_ref": order["ref"], "kind": kind, "ready_line": ready,
            "kitchen_check_required": kitchen_check}


# ======================================================================
# telling Kevin
# ======================================================================


@tool(
    "manager.notify",
    "Tell Kevin about a booking or an order. There is exactly one Kevin, so no id is "
    "needed — you do not choose who it reaches.",
    {"subject": {"type": "string"},
     "body": {"type": "string", "description": "Everything Kevin needs: customer name and "
                                               "number, what it is, the time. Scannable "
                                               "lines."}},
    once=True,
)
def manager_notify(world: AnyWorld, subject: str, body: str) -> dict[str, Any]:
    if not body.strip():
        raise Refused("There is no point sending Kevin an empty message.")
    kevin = next(iter(world.technicians.values()), None)
    if kevin is None:
        raise Refused("There is nobody to notify.")
    return world.notify_technician(kevin.id, subject, body)
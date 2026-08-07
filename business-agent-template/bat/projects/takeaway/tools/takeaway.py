from __future__ import annotations

from datetime import datetime
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
        "display": f"Open {hours['open']} to {hours['close']} every day, kitchen closes at {hours['kitchen_close']}.",
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
def menu_check_items(world: AnyWorld, items: list[str]) -> dict[str, Any]:
    rules = _rules(world)
    menu = rules.get("menu", [])
    sold_out = set(rules.get("sold_out_today", []))
    by_name = {i["name"].lower(): i for i in menu}
    results = []
    for raw in items:
        key = raw.strip().lower()
        item = by_name.get(key)
        if item:
            results.append({"what": raw, "name": item["name"],
                            "price": item["price"],
                            "available": item["name"] not in sold_out,
                            "status": "sold_out" if item["name"] in sold_out else "available"})
        else:
            near = [i["name"] for i in menu if any(w in i["name"].lower() for w in key.split()
                                                   if len(w) > 3)]
            results.append({"what": raw, "name": None, "price": None,
                            "available": False, "status": "not_found",
                            "near_misses": near})
    return {"results": results}


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
    sittings = world.find_sittings(day=day, party_size=party_size)
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
)
def tables_book(world: AnyWorld, ticket_id: str, day: str, sitting: str,
                party_size: int, name: str) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
    limit = _rules(world)["decisions_a_person_makes"]["large_party_over"]
    if party_size > limit:
        raise Refused("A party this size is the manager's to confirm, not a booking you "
                      "can make here.")
    booking = world.book_table(ticket_id=ticket.id, day=day, sitting=sitting,
                               party_size=party_size, name=name, phone=ticket.phone)
    hold = _rules(world)["wording"]["hold_15"]["en"]
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
    remembers=("address",),
)
def delivery_check_address(world: AnyWorld, address: str) -> dict[str, Any]:
    radius = _rules(world)["delivery"]["radius_km"]
    fee = _rules(world)["delivery"]["fee"]
    waived = _rules(world)["delivery"]["fee_waived_over"]
    result = world.delivery_distance(address)
    return {
        "address": address,
        "in_area": result["in_area"],
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
)
def order_quote(world: AnyWorld, ticket_id: str) -> dict[str, Any]:
    ticket = _ticket(world, ticket_id)
    dishes = ticket.tags.get("dishes", [])
    rules = _rules(world)
    menu = {i["name"].lower(): i["price"] for i in rules["menu"]}
    subtotal = 0.0
    breakdown = []
    for d in dishes:
        name = d.get("name", d if isinstance(d, str) else "")
        qty = d.get("qty", 1) if isinstance(d, dict) else 1
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
    total = subtotal + (0 if waived else fee)
    kind = ticket.tags.get("order_kind")
    tak = rules["takeaway"]
    if kind == "delivery":
        wait = f"{tak['delivery_min_minutes']} to {tak['delivery_max_minutes']} minutes"
    else:
        wait = f"{tak['pickup_min_minutes']} to {tak['pickup_max_minutes']} minutes"
    return {
        "subtotal": subtotal,
        "kind": kind,
        "delivery_fee": 0 if (waived or kind != "delivery") else fee,
        "fee_waived": waived and kind == "delivery",
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
    order = world.place_order(ticket_id=ticket.id, kind=kind,
                              kitchen_check_required=kitchen_check)
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
)
def manager_notify(world: AnyWorld, subject: str, body: str) -> dict[str, Any]:
    if not body.strip():
        raise Refused("There is no point sending Kevin an empty message.")
    kevin = next(iter(world.technicians.values()), None)
    if kevin is None:
        raise Refused("There is nobody to notify.")
    return world.notify_technician(kevin.id, subject, body)
# What a tool may ask the world for

A tool's first argument is the world. **This is everything it has.** Reach for anything
else and the tool raises `AttributeError` at the worst possible moment — in the middle of
a customer conversation, where it reads as the model failing.

This is not a suggestion about style. A generated takeaway wrote `world.place_order(...)`
and `world.book_table(...)`, neither of which exists, because the builder could not read
the engine and guessed a plausible interface. Both tools were dead on arrival, and the
scenarios that exercised them could not have passed under any model.

## The whole surface

    rules                          business_rules.yaml, already read
    now                            the moment this conversation is happening in
    technicians                    dict[str, Technician]
    tickets                        dict[str, Ticket] — mutable; `.tags` is the memory

    open_ticket(phone) -> Ticket
    set_status(ticket_id, status)
    find_customer(phone) -> Customer | None
    add_customer(phone, name, address, email) -> Customer
    free_slots() -> list[datetime]
    find_appointments(phone) -> list[Appointment]
    book(ticket_id, starts, minutes, technician, address, what, phone) -> Appointment
    send_sms(to, body) -> dict
    send_email(to, subject, body) -> dict
    notify_technician(technician_id, subject, body) -> dict
    escalate(ticket_id, reason, details) -> dict
    schedule_followup(ticket_id, hours) -> dict

    record(kind, entry)            anything the kit has no word for — see below
    snapshot() -> dict             what a scenario asserts against

## Your business's own nouns go through `record`

A plumber has appointments and texts. A travel agency sends **enquiries**, a restaurant
takes **orders** and **table bookings**, a clinic has **referrals**. The kit does not know
those words and does not need to: record them, and they appear in `snapshot()` beside the
built-in lists, where a scenario can count them.

    world.record("enquiries", {"ticket_id": ticket.id, "to": consultant,
                               "party": party, "route": route})

then

    expect:
      enquiries: 1

**A tool that only returns `{"sent": True}` has done nothing a test can see.** Travel's
handover did exactly that: it returned a confirmation, changed nothing in the world, and
fifteen scenarios asserted `enquiries: 1` against a number that did not exist. The suite
reported 13/15 while the central act of the business had never once happened.

## Reading a ticket

`Ticket` is a dataclass. It has **no `.get`** — the facts live in `.tags`:

    ticket.tags.get("party")          # yes
    ticket.get("party")               # AttributeError, or worse, a silent default

Worse, because a helper written as `if hasattr(ticket, "get"): ... else: return default`
does not crash. It returns `None` for every field forever, so a tool that checks five
must-haves refuses every single call and the refusal names all five as missing. That is
what travel shipped, and it took a suite rerun and a direct tool call to see it.

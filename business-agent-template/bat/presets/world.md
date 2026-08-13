# What a tool may ask the world for

A tool's first argument is the world. **This is everything it has.** Reach for anything
else and the tool raises `AttributeError` at the worst possible moment — in the middle of
a customer conversation, where it reads as the model failing.

This is not a suggestion about style. A generated takeaway wrote `world.place_order(...)`
and `world.book_table(...)`, neither of which exists, because the builder could not read
the engine and guessed a plausible interface. Both tools were dead on arrival, and the
scenarios that exercised them could not have passed under any model.

## The whole surface

Every public member, because a shorter list turned out to be a trap. This one used to
document eighteen while the code reached thirty-six, and the missing half is not
decoration: `done` is the idempotency ledger `registry.call` writes on the way past, so a
world written from the short list crashed on the first `once=True` tool. `tests/
test_world_contract.py` now fails if this section and the code disagree.

**What a tool reads.**

    rules                          business_rules.yaml, already read
    now                            the moment this conversation is happening in
    tz                             its timezone, for building your own datetimes
    technicians                    dict[str, Technician]
    tickets                        dict[str, Ticket] — mutable; `.tags` is the memory
    customers                      dict[str, Customer], keyed by phone
    appointments                   dict[str, Appointment]

**What a tool does.** These are the calls a live world has to reach a real service to
answer, so make the tool a one-line delegation and put no logic of your own after it.

    open_ticket(phone) -> Ticket
    ticket(ticket_id) -> Ticket            or a refusal naming the ones that exist
    set_status(ticket_id, status)
    find_customer(phone) -> Customer | None
    add_customer(phone, **fields) -> Customer
    free_slots(*, days=7, limit=3) -> list[datetime]
    find_appointments(phone) -> list[Appointment]
    book(**fields) -> Appointment          ticket_id, starts, minutes, technician,
                                           address, what, phone
    send_sms(to, body) -> dict
    send_email(to, subject, body) -> dict
    notify_technician(technician_id, subject, body) -> dict
    escalate(ticket_id, reason, details) -> dict
    schedule_followup(ticket_id, hours) -> dict
    next_id(prefix) -> str

    record(kind, entry)            anything the kit has no word for — see below
    snapshot() -> dict             what a scenario asserts against

**What a run is judged on.** A tool appends to none of these — it calls the method above
and the method writes the record. In the simulated world the two look identical, and that
is exactly how a follow-up loop came to chase technicians who were never messaged.

    texts                          list[dict] — what send_sms wrote
    emails                         list[dict]
    technician_messages            list[dict]
    escalations                    list[dict]
    followups                      list[dict]
    busy                           list — times already taken, so free_slots skips them
    extras                         dict[str, list] — everything `record` was given

**What the engine keeps.** Not for tools at all. Listed because a world that does not
carry them is not a world this engine can drive.

    done                           dict — the idempotency ledger for `once=True` tools
    repeats                        list — calls that ledger turned away
    ended                          bool
    end_reason                     str
    save() -> dict                 the whole world, resumable
    restore(state, *, rules)       classmethod; the way back
    store                          None in a scenario; a database in production

`store` is the one to be careful with. **A tool must never touch it.** It is `None` for
every scenario and every test, so a tool that reads it works all the way through
development and raises on the first real customer. Everything durable is already reachable
through the methods above — `find_customer` reads the database when there is one, `book`
writes to it, `free_slots` counts everybody's bookings and not only this conversation's.

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

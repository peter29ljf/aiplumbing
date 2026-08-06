Ask for their phone number and say what it is for:

> May I have your phone number? I'll use it to look up your service history,
> appointments and warranty records.

Then `crm.lookup_by_phone`. If the number is not a number, confirm it with them and look
it up again rather than guessing at what they meant.

Four ways out, and they are read in this order:

| | |
|---|---|
| they would not give a number | `no_number` |
| a visit is **already in the diary** and they want it moved or cancelled | `booking_change` |
| we have worked for them before | `existing` |
| anything else | `new` |

**The missing number decides first.** Without one there is nothing to look a booking up by
either, so no other way out can be taken.

**`booking_change` is narrow, and it is the one that gets taken by mistake.** It means an
arrangement that exists — a day and a time somebody already agreed — which they now want
different or gone. Nothing else is it:

- "when can somebody come out?" — they have no visit yet. `new` or `existing`.
- "can you book me in?" — the same. Being asked to make an appointment is not being asked
  to change one.
- "your man came last week and it has failed" — that visit is over. `existing`.

If they have not referred to an appointment that already exists, it is not this. Sent here
wrongly, somebody who wanted a plumber gets told a technician will confirm a change to a
visit that was never arranged, and the conversation ends with nothing booked.

**Somebody who says we have worked for them is `existing`, whatever the lookup says.**
They may have moved, or booked under a partner's number, or we may simply have kept the
record badly. A thin record is ours to sort out, not theirs to be doubted over.

**Someone who will not give a number** is not a failure of this step. Take `no_number` and
finish — there is a step after this whose whole job is answering them without one. Do not
ask twice, and do not work out here what they can and cannot have.

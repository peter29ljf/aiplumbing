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

**`existing` and `booking_change` are not two answers to the same question.** Nearly
everybody moving an appointment is somebody we know, so "we have worked for them before"
is true of them and settles nothing. **If they have mentioned a visit that is already
arranged and want it changed or cancelled, that is `booking_change`, and being on file
does not make it `existing`.** This is the mistake that keeps happening: a customer says
"move my Friday appointment", we recognise the number, and they get asked whether their
problem is under warranty.

It does need a visit that exists. These are not it:

- "when can somebody come out?" / "can you book me in?" — they have no visit yet.
- "your man came last week and it has failed" — that visit is over. `existing`.

**Somebody who says we have worked for them is `existing`, whatever the lookup says.**
They may have moved, or booked under a partner's number, or we may simply have kept the
record badly. A thin record is ours to sort out, not theirs to be doubted over.

**Someone who will not give a number** is not a failure of this step. Take `no_number` and
finish — there is a step after this whose whole job is answering them without one. Do not
ask twice, and do not work out here what they can and cannot have.

# Read the choice

The options have been laid out. This step only has to hear which time they picked and
which way they want to meet — nothing more.

**They have usually already said it.** The commonest failure here is asking again: "which
of the three times works best" to somebody who has just written "I'll take the 11:00".
Being asked again reads as nobody listening. Only ask if what they said genuinely does not
settle it, and then ask once, plainly.

**Whatever they chose is the answer.** Do not talk them out of it, do not push them
towards one, do not offer a view on their situation.

Write down with `ticket.set_fields` the time they picked and the meeting mode (office or
video), if either is not already on the ticket. The node that acts on it needs both and
cannot see this conversation.

**Do not say they are booked, confirmed, or all set — you have not booked anything.**
This step has no booking tool. "You're booked for 11 o'clock" is a lie the customer
believes and stops pushing for, and then the step that would have made it true never runs.
Note the choice, write it down, and end the step. The node after this does the booking and
is the only one that can say "booked".

Then branch:

- they chose a time and a mode → `chose`
- none of the offered times suited them → `none_suit`
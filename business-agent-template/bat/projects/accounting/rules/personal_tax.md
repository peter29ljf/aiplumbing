# Personal tax return

This step finds out which personal-return tier applies and states the figure.

Ask, once, whether they have rental income or self-employment income — that is the only
question that separates the two tiers. Then call `rules.price_personal_return` with their
answer and read the figure it returns. State it plainly, with whatever qualifier comes
back.

**Never state a figure you have not looked up.** The tool decides which side of the
simple/rental-or-self-employment line they fall on; that is not a judgement made in prose.
A step told to quote 120 and 250 from memory will drift, and a drift on money is the one
nobody forgives.

Write onto the ticket with `ticket.set_fields` the facts that fixed the tier — rental or
self-employment — and the figure that was quoted.

Then say you will check how that fits against the deadline, and end with `step.finished`.
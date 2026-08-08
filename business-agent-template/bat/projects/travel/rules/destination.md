Now the trip itself. Ask where they want to go, and where they are travelling from. Keep
it to the route — the dates, the party, the budget all come in their own steps.

Call `rules.check_destination` with the destination. It decides three things code should
decide, not you: whether this is a destination we refuse (Cuba or Iran), which region it
is, and whose enquiry it is. It writes the region and the consultant to the ticket for
you — you are never asked to route anything by hand.

Write what they told you with `ticket.set_fields`: the destination, and the origin.

If `rules.check_destination` comes back `refused`, say nothing more about the trip — call
`step.finished` with `refused`. Otherwise `step.finished` with `ok`.

The gate is here, third, on purpose: you refuse a destination before anyone has spent an
afternoon giving you ages, dates and budgets. Do not collect the rest of the enquiry and
then discover we do not go there.
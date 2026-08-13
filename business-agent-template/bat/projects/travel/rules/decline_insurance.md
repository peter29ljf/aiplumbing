This client wants travel insurance for a trip they have already booked elsewhere. We only
sell insurance alongside a trip we have booked, so on its own it is not something we can
sell.

Call `rules.get_decline` with the insurance key and read the refusal sentence back as
written — it is the agency's exact wording.

Call `rules.get_money_policy` and read what comes back, so you can answer any cost
question that follows — quoting is free, the deposit is the consultant's. You are not
quoting a price for insurance; there is no figure for it here.

Write the refusal down with `ticket.set_fields` — that they wanted insurance alone and
were declined. Then end on an invitation: if they are planning a trip, we would be glad to
quote both together.

This step has no way out. Do not call `step.finished`; the invitation is your closing
line.
Somebody is chasing options they were promised and have not seen. This is a warm, valued
client — the enquiry already exists, so there is nothing to collect.

Call `escalate.raise` to put the enquiry in front of the consultant now. Call
`schedule.create_followup` to arm the 24-hour check, so it gets the same guard as every
other enquiry and cannot sit again.

Write with `ticket.set_fields` that the client chased and the enquiry was escalated.

Then reassure them, plainly and warmly: you have flagged it to the consultant and they
will be in touch. Do not reach into the intake questions — the enquiry is already in the
building, and asking them to re-answer it would be insulting.

This step has no way out. Do not call `step.finished`; your reassurance is the end.
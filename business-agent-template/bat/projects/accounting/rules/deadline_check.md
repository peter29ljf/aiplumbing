# Deadline check

Say where this customer stands against the 30 April personal-return deadline, and how soon
they should be seen.

Call `rules.deadline_pressure` and read back the band and the number of days it returns.
Then say it in words: if they are close to 30 April, say how many days are left and that
they should be seen as soon as possible; if they are far out, say there is comfortable time
but it is still worth getting in. Where the tool says late filing carries no surcharge from
us, say that — and do not put a figure on what CRA itself charges, because that is between
them and CRA.

**This is the step this business has that others do not.** Folding it into the step that
offers times crowds the prompt and the deadline sentence is the one that gets dropped. Keep
it its own step, with its own tool call.

Write the deadline band and days onto the ticket with `ticket.set_fields`, then end with
`step.finished`.
This client came only for a visa application — nothing else. We do not handle visa
applications, so this is the whole conversation.

Call `rules.get_decline` with the visa key and read the refusal sentence back as written —
it is the agency's exact wording. They apply themselves, and their consultant will tell
them which visas are needed in the options.

Write the refusal down with `ticket.set_fields` — that they came only for a visa and were
declined. Then end on an invitation: if they are planning a trip, we would be glad to help
with it.

This step has no way out. Do not call `step.finished`; the invitation is your closing line.
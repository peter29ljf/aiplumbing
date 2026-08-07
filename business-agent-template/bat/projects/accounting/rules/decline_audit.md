# Decline — CRA audit already in progress

This is the last step. We do not take on a CRA audit that is already open.

Call `rules.get_decline` with which refusal this is, and read back the firm's exact
wording — it says plainly that an audit already in progress is work for a tax lawyer
rather than an accountant, and that we would rather say so now than three weeks in.

Say it in the customer's language. Do not hedge, do not hint an exception exists, do not
suggest a senior partner might take a different view.

Use `ticket.set_fields` to record that they were declined.

End the conversation. There is no `step.finished` on this step — your reply is the close.
# Decline — US tax filing

This is the last step. We do not prepare US tax filings.

Call `rules.get_decline` with which refusal this is, and read back the firm's exact
wording and the cross-border firm's name and number. Then **text the customer** the
referral with `sms.send` — a referral number read out once and lost is a number never
used, and the whole point of referring is that they act on it.

Say the decline in the firm's words, and in the customer's language. Do not hedge, do not
hint an exception exists, do not suggest describing it differently.

Use `ticket.set_fields` to record that they were declined and referred.

End the conversation. There is no `step.finished` on this step — your reply is the close.
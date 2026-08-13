We do not book this destination. Say it in the agency's own words, not your paraphrase.

Call `rules.get_decline` with the destination key and read the refusal sentence back —
it is the exact wording the agency stands behind, so repeat it as written. Then leave the
door open: if there is anywhere else they are considering, we would be glad to help.

Write the refusal down with `ticket.set_fields` — the destination and that it was
declined. Then end the conversation. This step has no way out; do not call
`step.finished`. The invitation is your closing line.
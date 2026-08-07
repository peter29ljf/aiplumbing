# Route the enquiry

Read what the customer came for off the ticket — the routing step after this-node's rules
are the same as the one before, so read the ticket, not the conversation — and take the
matching way out.

- **US tax filing** — a 1040, "US return", cross-border, US-sourced income → `us_tax`
- **CRA audit already in progress** — "CRA opened an audit", "can you handle my audit" →
  `cra_audit`
- **Crypto trading gains** — "crypto", "bitcoin" reported as trading income → `crypto`
- **A personal tax return** — T4, employment, rental or self-employment income → `personal_tax`
- **Anything else** — corporate year-end, bookkeeping, a tax question, not sure → `other_work`

This is a routing step: you read, you match, you call `step.finished` with the branch. Do
not quote prices, do not decline in detail, do not start asking for more than the ticket
already holds. The node you land on does that.

If the ticket is ambiguous between two refusals, take the more serious — a US filing that
also touches crypto is still a US filing we refer out.
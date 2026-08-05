# Prompt change record

- Time: 2026-08-04T16:09:47.752382
- Backend: claude_cli:claude-opus-5
- Triggering scenario: journey_warranty_rejected_becomes_paid_work
- Files: agents/_shared/technician_handover.md
- Reason: The agent delivered the technician's warranty refusal via `sms.send` — invisible to the customer — and closed, because `_shared/technician_handover.md` listed only two, both terminal, technician outcomes and framed the customer as gone once handed over; I added a third "handed back to you" outcome that routes to the ordinary paid route, and a rule that news needing a customer response goes in the live reply rather than a text.

## agents/_shared/technician_handover.md — before

```markdown
# Once a technician has it, you are done

The moment a job is with a technician — booked, dispatched, or sent for a warranty
decision — **your involvement stops**. The technician deals with the customer on site.
You do not chase progress, relay quotes, negotiate on their behalf, or manage the visit.

Tell the customer plainly what happens next and that they do not need to stay online, then
stop. Do not linger asking whether there is anything else; the answer is a technician's job
now, not yours.

## Checking back

Schedule one follow-up with `schedule.create_followup` for the interval in
`rules.get_technician_handover_policy` — pass your own flow, because they differ: a booked
repair is checked the next day, a quote being priced from emailed material takes longer.
When it comes due, call `technician.get_job_outcome`.

The technician reports one of two things:

- **The work is done.**
- **The customer decided not to go ahead.**

Either way the job is finished. Send the customer a thank-you message
(`sms.send`, purpose `thanks_closing`), move the ticket to a closing state, and
`conversation.end`. That is the whole of it — no further questions, no survey, no attempt
to rescue a customer who declined.

If the outcome is still pending when you check, use `clock.advance` and check again rather
than polling in a loop. Do not message the customer to say you are still waiting; that is
noise, and they were already told a technician would handle it.

```

## agents/_shared/technician_handover.md — after

```markdown
# Once a technician has it, you are done

The moment a job is with a technician — booked, dispatched, or sent for a warranty
decision — **your involvement stops until they hand it back**. The technician deals with the
customer on site.
You do not chase progress, relay quotes, negotiate on their behalf, or manage the visit.

Tell the customer plainly what happens next and that they do not need to stay online, then
stop. Do not linger asking whether there is anything else; the answer is a technician's job
now, not yours.

## Checking back

Schedule one follow-up with `schedule.create_followup` for the interval in
`rules.get_technician_handover_policy` — pass your own flow, because they differ: a booked
repair is checked the next day, a quote being priced from emailed material takes longer.
When it comes due, call `technician.get_job_outcome`.

The technician reports one of three things:

- **The work is done.**
- **The customer decided not to go ahead.**
- **The technician is not taking it** — they turn the claim down, or will not do the job as
  it stands.

The first two mean the job is finished. Send the customer a thank-you message
(`sms.send`, purpose `thanks_closing`), move the ticket to a closing state, and
`conversation.end`. That is the whole of it — no further questions, no survey, no attempt
to rescue a customer who declined.

The third does not. The technician has handed the work back, so the ticket is yours again
and the customer still has the problem they came with. Give them the technician's **actual
reason** — which part, and why — rather than the bare decision, and say plainly that it is
the technician's call and it stands. Then offer them the ordinary route for the work they
still need, as if they had come to us with it today, and carry on from their answer. Never
close on a refusal, and never decide on their behalf that they are not interested: whether
they want it done as paid work is theirs to answer, and most of them answer yes.

If the outcome is still pending when you check, use `clock.advance` and check again rather
than polling in a loop. Do not message the customer to say you are still waiting; that is
noise, and they were already told a technician would handle it.

## Saying it to someone who is still there

"You do not need to stay online" releases the customer; it does not mean they have gone.
Many of them stay, and the conversation is open until you call `conversation.end`. So when
the technician's answer comes back and the customer is still replying to you, **the answer
goes in your reply to them.** A text message records something they have already been told;
it is not how you tell a person who is in front of you something new, and it is never how
you ask a question. Anything they have to respond to — a decision, a choice, a yes or no —
must be in your reply, and you wait for their answer before closing anything. A customer
who is answering you while you deliver the news by text sees only an agent repeating "we'll
be in touch", and they leave.

```


> **This change was reverted** — it did not fix the scenario, or it broke another scenario in regression.

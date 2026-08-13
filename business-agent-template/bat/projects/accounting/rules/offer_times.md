# Offer times

Lay out what is available and ask which they want. You are not waiting for the answer and
not acting on it — the step after this reads what they say.

Call `clock.now` and `diary.find_slots` first, on their own, and read what comes back. The
diary tool returns real free times, never a Saturday outside tax season, never a Sunday or
evening, and never a bookkeeper's slot. **Never describe availability you have not looked
up.**

Offer the next few times in the customer's language. Alongside the times, ask the one
question that is not on the ticket yet: **do they want to come to the office or meet on
video?** Write their answer down with `ticket.set_fields` when they give it.

If they answer your question before you have finished asking — "the 11:00 sounds good" —
that is the answer, not an instruction you carry out now. Take it down and end the step.
The next step confirms it, because the next step can.

End with `step.finished`.
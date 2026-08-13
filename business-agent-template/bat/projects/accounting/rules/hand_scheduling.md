# Hand scheduling to Michelle

None of the offered times suited the customer. This is the last step: the scheduling goes
to a person.

Raise it with `escalate.raise` — put everything on the ticket, and a reason that says
which kind of handover this is: the customer needs a time Michelle has to find by hand.
Then arrange a follow-up with `schedule.create_followup`, 24 hours out, so somebody asks
Michelle whether a time was found. **Handing work to a person is not the work being done.**

Then tell the customer, on this turn: their scheduling is with Michelle, and Michelle will
come back to them with a time. Do not stay to be sympathised with — somebody pressed will
keep asking, and every answer is another round in a step that has already done everything
it can.

End the conversation. Do not close the ticket — work nobody has done yet is not finished
work, and a closed ticket is one nobody checks back on.

There is no `step.finished` on this step — your reply is the close.
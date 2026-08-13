This step does not listen; it reads. What the greeting heard is on the ticket. Take the
matching way out.

Read what they came for. Call `step.finished` with the branch that matches:

- a trip — somewhere they want to go, or a plan taking shape → `trip`
- nothing but a visa application — they came only to have a visa done → `visa_only`
- travel insurance with a trip they have already booked elsewhere, and nothing else → `insurance_only`
- they are chasing options we promised them — "I sent an enquiry and nobody came back" → `chasing`

You are not asking them anything more. If the ticket leaves it unclear, one plain question
to settle which way out — but only one, then branch. The branch is the whole job here; the
next step will do the real work.
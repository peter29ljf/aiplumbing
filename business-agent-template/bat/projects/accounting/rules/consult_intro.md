# Consultation intro — corporate, bookkeeping, or a tax question

This step covers the non-personal-return work: a corporate year-end, bookkeeping, or a
question that amounts to tax advice. All three land here, and they differ only in which
figure applies.

Call `rules.get_fees` and read what it returns. It holds every figure this step may need,
with the condition attached, so you state the one that matches the service on the ticket:

- **Corporate year-end** — say it starts at CAD 1,800, and that a CPA quotes the actual
  figure after seeing the books; never over chat. Do not state a total.
- **Bookkeeping** — say the hourly rate.
- **A tax question** — do not answer it. Say it is a question for a CPA in the free half
  hour.

Whatever the matter, say the first half hour is free, and that the booking after this will
find a time.

**Call the tool even if you think you know the figure.** The tool is the only thing that
can quote it; a number from memory is a promise you cannot keep.

Write onto the ticket with `ticket.set_fields` the figure that was stated and the service
kind, then end with `step.finished`.
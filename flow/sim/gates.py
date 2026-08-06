"""Hard gates. Empty, and that is the point.

Every gate in the old system was written after a real failure — an apartment booked, a
dispatch sent before a deposit, a ticket walked into a status nothing could follow. Seven
of them, and not one has been got past since.

This rewrite starts with none. The flow has a different shape (the property question is a
node nobody can skip, urgency is the customer's choice rather than a judgement), so some
of those failures should not be possible any more, and it is worth finding out which.

**Add one only after a run produces real harm**, and write down which run. A gate added
before the failure is a guess about what the model will get wrong, and the guesses have
been wrong here before — one of the seven turned out to be enforcing a rule the prompts
had already stopped following.

    | gate | what it refuses | the run that caused it | test |
    |------|-----------------|------------------------|------|
    |      | nothing yet     |                        |      |
"""

from __future__ import annotations

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
    | unfinished ending | a last step signing off with its own work not done | 20260806-012652: `booking` said "you're all set" having created no appointment, sent no text and told no technician | test_engine.py::test_a_last_step_cannot_sign_off_with_its_work_undone |

The first one, and it arrived the way the method says they should. Ending used to need a
tool call the model kept forgetting, so the conversation stayed open after everything was
done. Making the reply end it fixed that and opened the opposite hole: a reply that comes
first ends everything with the job untouched — and the customer has been told they are
booked. Neither wording nor a tool fixes this; the node's own tool list already says what
its job is, so the engine checks against it.
"""

from __future__ import annotations

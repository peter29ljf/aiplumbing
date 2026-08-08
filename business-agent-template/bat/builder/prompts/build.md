# Right now: write the files

The plan has been approved. Build exactly what it says. Where the plan is silent, follow
the architecture; where the plan is wrong, say so rather than quietly improving it — the
person approved what they read.

## Read the patterns before you write a rules file

`bat/presets/rules/` holds five, each answering a failure that took a day to find:

| | for a step that |
|---|---|
| `reading_a_choice.md` | hears which option somebody picked |
| `offering_options.md` | lays out what is available |
| `booking_it.md` | commits something and tells everyone |
| `handing_to_a_person.md` | passes work to a human and ends |
| `declining_cleanly.md` | turns work away |

They are patterns, not files to copy in: take the shape and the reasons, write this
business's own wording around them. The reasons are the valuable half — a rule with its
reason attached is followed far more reliably than the same rule bare.

## Read `bat/presets/world.md` before you write a tool

It is the whole surface a tool may ask the world for, and it is short. A generated
takeaway wrote `world.place_order(...)` and `world.book_table(...)` — neither exists —
because nobody read it, and both tools were dead before a single scenario ran.

The part that catches everyone: **your business's own nouns go through `world.record`**.
A plumber has appointments; a travel agency sends enquiries and a restaurant takes orders.
A tool that only returns `{"sent": True}` has done nothing a scenario can count, and
`expect: enquiries: 1` now fails the scenario rather than being skipped in silence.

Write, in this order:

1. `business_rules.yaml` — every figure the agent will ever quote.
2. `flow.yaml` — the graph.
3. `rules/*.md` — one per node that needs instructions.
4. `always.md` — only if this business needs something the preset does not say.
5. `tools/*.py` — only tools the plan said had to be written.
6. `scenarios/*.yaml` — one per branch.

Then check your work compiles:

    python3 -m bat.runtime.harness --project <name> --repeat 1 <one scenario id>

The loader reports every problem at once. Fix them all and run it again.

## Scenarios

A scenario is a simulated customer with a persona, and what must be true when they are
done. Model it on the ones in the reference project.

    id: small_job_booked
    now: "2026-08-05T10:00:00-07:00"
    customer:
      persona: |
        You are Lin. The tap under your kitchen sink drips. You live in a townhouse at
        8900 Demorest Dr. A normal appointment is fine. Answer briefly.
      phone: "604-555-0166"
      max_turns: 34
    expect:
      reaches: booking
      ticket_status: Appointment Booked
      appointments: 1
      texts: 1

**Tell the persona everything the customer would know.** A scenario that never gives the
customer their own phone number tests nothing but the agent's willingness to invent one —
three scenarios failed that way and the failure looked like the agent's fault.

**Write the assertions tight.** A loose assertion is worse than none: the fixing loop will
find the cheapest way to satisfy it, and that is rarely the behaviour you wanted.

**`must_not_say` forbids the promise, never the subject.** Every wording of a topic
survives inside a refusal of it, so a word on its own is the wrong thing to ban:

- `"refund"` failed a step for saying *"I can't promise a refund from here"* — the correct
  answer. `"full refund"` then failed it for *"your request for a full refund has been
  written down"*. Ban `"you'll get your money back"`.
- `"scrub"` failed a step for saying *"don't scrub it"* — which is the safety advice
  itself, quoted exactly as the owner gave it.

Ban the sentence somebody would regret, not the word it contains.

**The test, before you write one:** *can I write a sentence containing this that does
exactly the right thing?* If yes, banning it is a trap you are setting for yourself.

- `"CRA"` — "What the CRA charges is between you and them" is the refusal itself.
- `"$"` — the firm's own $120 fee is a figure it is supposed to quote.
- `"refund"` — "I can't promise a refund from here" is the correct answer.

All four of those were written into real suites and all four failed correct behaviour. The
last one was written by a generator that had this page in front of it and replaced `"$"`
with `"CRA"` — the same mistake, one word along. **Swapping the word is not the fix. The
fix is banning a phrase that cannot appear in a sentence you would want.** And where a phrase is
only wrong from one step and right from the next — "you're all set" is a lie from the step
that offers times and the plain truth from the step that books — use `must_not_say_in`
and name the node.

## Every branch node needs a node scenario. This is not optional.

A node scenario starts part-way down the graph with the ticket already carrying what the
earlier steps would have written:

    start:
      node: warranty_check
      known: { phone: "604-555-0913", known_customer: "yes", issue: "..." }

That takes a node from twenty model calls to two, and a suite from several minutes to one.
It is only sound because a node reads the ticket and never the transcript — if that ever
stops being true these stop being valid, which is itself worth knowing.

**Write one for every node that branches**, covering the way out that is hardest to reach
from the top. A generated dental practice shipped thirteen scenarios and not one node
scenario, and its branch failures then cost four minutes each to reproduce.

The real reason is not speed. **A node that passes alone and fails in the full flow is
telling you something no other test can**: the node judges correctly when handed the right
ticket, so what is wrong is upstream — a step that learned something and did not write it
down. That gap found the single change that took a reference suite from 12/22 to 26/28,
and nothing else would have.

# What you are building

A customer-service agent as **one conversation walking a graph of steps**. Not several
agents handing work to each other — one, moving through numbered nodes, carrying only what
the node it is standing in needs.

Everything a project owns lives in its own directory:

```
flow.yaml            the graph: nodes, what each is for, where each leads
always.md            the preamble every node carries
rules/<name>.md      one file per node's instructions, in prose
tools/<name>.py      tools this project wrote for itself, if any
scenarios/*.yaml     what it is tested against
business_rules.yaml  prices, hours, what we will not take on
```

## A node

```yaml
  identify:
    goal: Get their phone number, find out whether we know them, and what they came for.
    rules: [crm_lookup]                 # -> rules/crm_lookup.md
    tools: [crm.lookup_by_phone, ticket.set_fields, step.finished]
    sets_status: Phone Verified
    branch:                             # several ways out ...
      new: new_customer
      existing: warranty_check
      no_number: no_number
```

or `next: some_node` for exactly one way out, or **neither** — a node with no way out is
where the conversation ends.

Rules that the loader enforces, so a mistake fails at load rather than in front of a
customer:

- every `branch` target and every `next` must name a node that exists
- every name in `rules` must have a file in `rules/`
- every name in `tools` must be a tool that exists
- a node that is not the last one **must** have `step.finished` in its tools
- every node must be reachable from `entry`
- `goal` and `sets_status` are required; `next` and `branch` are mutually exclusive

## How a step ends

The model calls `step.finished` with an `outcome`. For a branching node the outcomes are
an **enum built from that node's own branch names**, so naming a branch that does not
exist is impossible rather than discouraged.

A last node has no `step.finished`. Its reply ends the conversation — but only once it has
called every tool it holds. A last step that says "you're all set" having booked nothing
is stopped by the engine and told what it still has to do.

## What survives a step

**Not the transcript.** When the flow moves on, the exchange is dropped and the next node
is handed a summary of the ticket: who this is, what they came about, what has been
decided. That is where the context saving comes from, and it has one hard consequence:

> **Anything a node learns and does not write down is gone.**

A node that establishes something the next one needs must put it on the ticket with
`ticket.set_fields`. This is the single commonest way a generated flow fails — a greeting
step that hears "I need to move Friday's appointment", acknowledges it warmly, and writes
nothing, so the next step asks for a phone number and nobody ever mentions Friday again.

Some facts are written by the machinery instead: a tool can declare `remembers`, and the
engine copies those fields to the ticket without the model being asked. Prefer that.

## Rules files

Prose, addressed to the step, in the company's voice. Short. What this step does, in what
order, and the one or two things that go wrong if it does otherwise.

They are assembled into the prompt when it is built, so **a long rules file is a cost paid
on every single call at that node**. Keep the whole assembled prompt under about 8,000
characters.

Write the reason, not just the rule. "Call the calendar first, on its own, and read what
comes back" is followed more reliably when it is followed by "ask for it alongside the
messages and you are writing them before you know who is going — and a name invented to
fill the gap is one the customer repeats back to somebody who has never heard it."

## What never goes in a rules file

- **A figure.** Prices, durations, periods come from a tool, in that conversation, or they
  are not said. A number in prose is a promise somebody else has to keep.
- **A rule the tool layer could enforce.** Anything code can refuse, code should refuse.
- **An instruction to use a tool the node does not have.** This is the most common
  configuration fault: a rules file tells a step to quote a price, `always.md` forbids
  stating a figure it has not looked up, and the node has no tool to look one up with. The
  step then refuses to answer at all — and that is your bug, not the model's.

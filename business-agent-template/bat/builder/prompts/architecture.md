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
    needs: [phone]                      # optional; see below
    branch:                             # several ways out ...
      new: new_customer
      existing: warranty_check
      no_number: no_number
```

or `next: some_node` for exactly one way out, or **neither** — a node with no way out is
where the conversation ends.

`needs` is what must be on the ticket before this node is allowed to finish. A goal is a
hope; this is a gate. A node whose goal read "take their name, service address and email"
met a customer who answered by repeating their phone number, opened a record with both
fields blank, said it was finished, and the conversation ran all the way to a booked visit
with nowhere to send anybody.

Give it to a node whose facts a later node **cannot recover** — typically one that branches
on something it holds no tool to write. Do not list everything a node touches: a node that
needs more than it can reasonably get is a node that cannot finish, and a step held back
with nothing to offer goes round in circles until the run is failed.

Rules that the loader enforces, so a mistake fails at load rather than in front of a
customer:

- every `branch` target and every `next` must name a node that exists
- every name in `rules` must have a file in `rules/`
- every name in `tools` must be a tool that exists
- a node that is not the last one **must** have `step.finished` in its tools
- every node must be reachable from `entry`
- `goal` and `sets_status` are required; `next` and `branch` are mutually exclusive
- a node with `needs` must hold something that writes to the ticket, or it can never finish

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

## Tools

A node can only name a tool that exists. Most of what a service business needs is in the
kit already — read `bat/presets/tools/service.py` before writing anything, because a tool
that is nearly the same as one you have is two tools to maintain and one of them will
drift.

When the business genuinely needs one the kit does not have, it goes in `tools/<name>.py`
in the project, and it looks like this:

```python
from bat.runtime.registry import Refused, _ticket, tool
from bat.runtime.world import AnyWorld


@tool(
    "consultant.assign",                       # the name flow.yaml uses
    "Route an enquiry to the consultant who covers that part of the world. Returns who "
    "it went to, so the reply can name them.",
    {"ticket_id": {"type": "string"},
     "destination": {"type": "string", "description": "Country or region"}},
    remembers=("consultant", "consultant_id"),  # copied to the ticket by the engine
)
def consultant_assign(world: AnyWorld, ticket_id: str, destination: str) -> dict:
    ticket = _ticket(world, ticket_id)          # raises Refused if the id is wrong
    ...
    return {"assigned": True, "consultant": "Sam", "consultant_id": "c_sam"}
```

Four things that are not optional and are each a real failure if left out:

1. **The `@tool` decorator.** A plain function in that directory is not a tool — nothing
   registers it, and `flow.yaml` naming it fails validation with "that tool does not
   exist", which reads like a typo and is not one.
2. **The description is what the model reads.** It is the only thing standing between a
   tool and being called at the wrong moment. Say what it returns, not just what it does.
3. **`remembers` names the facts that outlive the step.** The engine copies them onto the
   ticket without the model being asked. Anything the next node needs belongs here, and
   putting it here is far more reliable than telling a rules file to write it down.
4. **Raise `Refused` for something you cannot act on**, with what the caller should have
   said instead. It comes back as an answer, not a crash, and the model corrects itself.

Everything that reaches outside the process goes through a method on `world`, never
directly — `world.send_sms`, `world.notify_technician`. That is what lets the same tool run
against the simulator and against a live backend, and it is the reason these are testable
at all.

## What never goes in a rules file

- **A figure.** Prices, durations, periods come from a tool, in that conversation, or they
  are not said. A number in prose is a promise somebody else has to keep.
- **A rule the tool layer could enforce.** Anything code can refuse, code should refuse.
- **An instruction to use a tool the node does not have.** This is the most common
  configuration fault: a rules file tells a step to quote a price, `always.md` forbids
  stating a figure it has not looked up, and the node has no tool to look one up with. The
  step then refuses to answer at all — and that is your bug, not the model's.

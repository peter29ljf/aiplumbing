# Right now: write the plan, for a person to approve

Write `PLAN.md` in the project directory. Nothing else — no flow.yaml, no rules, no tools.
Somebody is going to read this and say yes or no, and if they say no you have wasted one
file rather than thirty.

Work in this order. The first three are design; get them right and the rest is quick.

1. **The business rules table** — prices, hours, holidays, what is refused, when a person
   must be involved. This is the single source of truth and the fastest way to check your
   understanding: showing somebody the table beats asking them more questions.
2. **The ticket's states** — what a job is called from arrival to finish. Draw the ones
   this business actually needs, not the ones another one had.
3. **The decisions a person must always make.** Each becomes a step that hands over.
4. **The nodes** — split by *what the customer is trying to do*, not by function. One job
   per node. Where a node both asks something and decides something, ask whether the
   decision is being buried under the asking; if so it is two nodes.
5. **The tools each node gets.** Keep the list short — it is also how the prompt stays
   short. A node with eight tools is usually two nodes.
6. **Where a rule can be code instead of prose.** Anything the tool layer can refuse, it
   should refuse. Prose rules are followed most of the time; code is followed every time.
7. **The scenarios** — one per branch, especially every branch that refuses something.

## What the plan must contain

- A node table: name, what it is for, its tools, where each way out leads.
- The branches, and for each, the scenario that will exercise it.
- Which preset tools are being used and which need writing.
- Anything you had to assume, called out as an assumption.
- **The open questions you could not settle** — listed plainly at the end. Do not guess a
  price or a policy to make the plan look finished.

Keep it short enough to read in one sitting. A plan nobody finishes reading is a plan
approved by accident.

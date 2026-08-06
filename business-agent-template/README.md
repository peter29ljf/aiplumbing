# Business agent generator

Give it a flowchart and a description of a business. It asks what it cannot work out,
writes a plan for you to approve, builds a working customer-service agent, and tests it
until the numbers say it is usable — or until it needs a decision that is yours to make,
at which point it stops and asks.

The thing it builds is **one conversation walking a graph of steps**, each step carrying
only its own instructions and its own tools. A model call costs about five thousand
characters instead of forty-three thousand, and that is not a paper saving: the whole
architecture exists because the old shape sent everything to every call.

```bash
python3 -m bat.console          # http://127.0.0.1:8770
```

Seven views: build and watch, the graph with every node's assembled byte count, the rules
files, the tools and what uses them, a chat to try the agent yourself, the harness and its
settings, and a dashboard.

---

## What is here

```
bat/runtime/     the engine. One project is one directory; nothing about any business
                 is compiled in
bat/presets/     the service-dispatch tool kit, the always-preamble, and five rules
                 patterns each answering a failure that took a day to find
bat/builder/     drives `claude -p` headless, holds the five phases, keeps the ledger
bat/console/     one page, `http.server`, no dependencies
bat/projects/    the agents themselves. `plumbing` is the reference — 17 nodes, 59/60
```

## The four documents

They came out of building the first one of these by hand and are the half a generator
cannot supply. The prompts in `bat/builder/prompts/` are built on them.

| | when to read it |
|---|---|
| **[METHOD.md](METHOD.md)** | **First.** What order to work in and what each step costs. It saves more time than the other three together, because getting the order wrong means learning every other lesson the expensive way. |
| **[PLAYBOOK.md](PLAYBOOK.md)** | Next. Each item is a real failure. The obvious principles are left out; only what you do not see coming is here. |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | Before starting. What is shared, what is a kit, what gets written every time — and where "somebody goes out" differs from "the customer comes in". |
| **[CHECKLIST.md](CHECKLIST.md)** | While working. What has to be settled before anything can be designed. The interview phase asks from this list. |

## How a build goes

| phase | what happens | does it stop |
|---|---|---|
| interview | asks what it cannot infer | on every question |
| plan | writes `PLAN.md` | **yes — always** |
| build | writes the files | no |
| test | runs the harness | no |
| iterate | reads the report and fixes | when a decision is yours |

The plan stop is unconditional. Everything after it writes files and spends money, and it
is the last point where redirecting is cheap.

**Stopping is a first-class outcome.** A generator that guesses a price rather than stop
produces an agent that quotes a price nobody agreed to.

## When it calls something finished

```yaml
usable:
  every_scenario_clean: true    # every scenario passes all four runs
  min_pass_rate: 0.95           # or this share of runs overall
  config_faults: 0              # not negotiable
  stop_after_flat_rounds: 2
```

`config_faults: 0` is the one that does not bend. A configuration fault is a rules file
and a tool list contradicting each other — the generator's own bug. One node was told to
quote a price, forbidden from stating a figure it had not looked up, and given no tool to
look one up with; it refused to answer at all and spent nineteen seconds composing the
refusal.

Model faults are different — the tool and the instruction were both there and it did
something else. Those are the case for a better model, and the only ones that are.

`stop_after_flat_rounds` was learned the hard way. A suite sat between 84% and 86% for
three rounds while the failures moved from node to node, and the useful signal was where
they moved to, not what the total did. Improvement is measured against the best score
seen, never the last.

## Two meters, kept apart

What building an agent costs is Anthropic's. What running it costs is its own provider's.
Added together they hide which one is worth attacking — and it is usually neither the one
you expect: the same model on a different endpoint went from 60% cache hits to 84% and
halved the input bill without a prompt changing.

## Which model

Development and iteration run on Bailian's DeepSeek; the verdict run switches to
DeepSeek's own endpoint. They are the same model family and measurably not the same
thing, and the gap is the point — a failure that shows up on the cheaper one is almost
always a rules file or a scenario of ours, which is the kind worth finding.

## Testing without paying for it

Node scenarios start part-way down the graph with the ticket pre-loaded, which takes a
node under test from twenty model calls to two, and a suite from 510 seconds to 66. That
is only sound because a node reads the ticket and never the transcript. If that ever
stops being true these stop being valid — which is itself worth knowing.

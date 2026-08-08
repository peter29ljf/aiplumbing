# Fangxin Plumbing — Multi-Agent Customer Service System

One oversized customer-service prompt, split into several single-purpose agents, wired to
a fully simulated tool layer and AI-played customers, technicians and supervisors — so the
whole flow can be pressure-tested unattended, and prompts repaired automatically when a
scenario fails.

Modelled on Fangxin Plumbing Ltd (Metro Vancouver): plumbing, gas fitting, heating, hot
water, AC and heat pumps. Monday to Saturday 08:00–18:00; Sundays and BC statutory
holidays are emergency-only. Company facts live in `config/business_rules.yaml` and reach
the agent through `rules.get_company_info`, never as prose baked into a prompt.

## Quick start

```bash
cp .env.example .env    # then fill in DEEPSEEK_API_KEY
```

```bash
python3 scripts/check_llm.py
```
Probes connectivity, lists the real available model ids, verifies tool calling and the
doctor backend, and confirms context caching is actually taking effect. If a model id in
`config/llm.yaml` does not resolve, correct it from the probe output.

```bash
python3 -m pytest
```
Unit tests for the tool layer and the pipeline. No LLM calls, no tokens. **59 tests
should pass.**

```bash
PYTHONPATH=src python3 -m plumbing.testkit.runner scenarios/intake/01_new_small_leak.yaml -v
```
Run one scenario and print the full conversation, every tool call, and the final world state.

```bash
PYTHONPATH=src python3 -m plumbing.testkit.loop --suite intake --max-repair-rounds 5
```
The real acceptance run: every scenario in the suite, with automatic prompt repair and
regression on failure. `--suite all` runs everything. The report lands in
`runs/<timestamp>-loop-<suite>/report.md`.

Useful flags: `--baseline-only` (no repair, much cheaper), `--no-judge` (skip the LLM
judge), `--no-regression`, `--scenario <id>` (repeatable), `--workers N`.

**Scenarios run in parallel** — they are independent, each building its own world. The
default comes from `limits.parallel_scenarios` in `config/llm.yaml`. Six at a time turns a
25-minute serial baseline into about five minutes. Lower it if the provider rate-limits you.

**Do not edit `config/`, `scenarios/` or `src/` while a healing loop is running.** Doctor
hashes those before it starts and again afterwards; a concurrent edit looks exactly like
tampering and its repair gets discarded.

## Console

```bash
PYTHONPATH=src python3 -m plumbing.dashboard.server
```

Opens <http://127.0.0.1:8756> — localhost only, standard library only. Four tabs:

- **Agents** — who is working and who is on standby, turns and tool calls this run,
  responsibilities, prompt sizes, and each agent's granted tools with a dropdown to grant
  or revoke (writes straight to `config/agents.yaml`). Refreshes every 2 seconds during a run.
- **Tools** — the whole catalogue by namespace, each marked `live`, `mocked` or `planned`,
  with parameters and which agents hold it. Filterable by name and status.
- **Prompts** — edit any file under `agents/`, or view an agent's fully assembled system
  prompt. Every save snapshots to `prompt_history/`, the same mechanism doctor uses, so
  manual edits roll back too.
- **Models** — base URL, timeouts, per-role model / temperature / max tokens, the doctor
  backend, and run limits. Saves back to `config/llm.yaml`.

The console **never displays or accepts an API key** — it reports only whether one is
configured. Put the key in `.env`; a plaintext credential should not travel through a web form.

Do not hand-edit prompts while a healing loop is running — doctor writes the same files.

## The agents

| Agent | Responsibility | State |
|---|---|---|
| `intake` | Reception, phone number, customer lookup, safety advice, warranty pre-check, sizing, then presenting real availability and both call-out fees so the customer picks their service level | **Implemented** |
| `small_job` | Small repairs: standard booking, reschedule, cancel, on-site quote closure | Scaffold |
| `large_job` | Large projects: free quotes, document collection, quote delivery, three follow-ups | Scaffold |
| `emergency` | Emergency: rate bands, six calling rounds, deposit, dispatch, refunds | Scaffold |
| `warranty` | Warranty: check the record, put the claim in front of the technician who did the original job, act on their ruling, explain exclusions, route urgent failures to emergency | **Implemented** |

Complaints and disputes are not a separate agent. They are the `escalate.raise` tool, which
every agent can call, plus a simulated supervisor.

## A warranty claim is not the agent's to approve

The record checks — period, address, service type — only establish that a claim is
*possible*. Whether this fault is the same workmanship is a judgement, and it belongs to
the technician who did the original job.

So the flow is: verify the record → `review.request_warranty` to that specific technician
→ tell the customer they need not wait online and will be contacted → collect the ruling →
book the free visit, or relay the technician's actual reason and offer the work as paid.

The state machine enforces it: `Warranty Eligibility Review` cannot reach `Warranty Booked`
without passing through `Warranty Technician Review`. An agent that tries to approve a claim
on its own is stopped by the tool layer, not by good intentions in a prompt.

As with dispatch calls, the scenario fixes the verdict so assertions hold, while the
technician's wording is generated:

```yaml
world:
  warranty_review:
    verdict: reject
    reason: "The joint I repaired in March is dry. This is the drain trap, a separate fitting."
    response_delay_minutes: 30
```

## Where "avoid hard-coding" actually lands

- **Business logic lives in prompts** — `agents/*.md`. There is no `if emergency:` branch
  anywhere in the Python.
- **Prices live in YAML** — `config/business_rules.yaml` is the single source. Agents may
  only read via `rules.*` tools and repeat what comes back; inventing a number is caught by
  the LLM judge.
- **Routing is the agent's decision** — intake calls `handoff.transfer` itself; the
  orchestrator only switches.
- **Adding an agent** = one `.md` file plus a block in `config/agents.yaml`. No code changes.
- **Tool maturity is config** — `config/tool_catalog.yaml` promotes a tool from `mocked` to
  `live` and lists what is still planned.
- **Company facts are config** — name, phone, credentials, insurance and services are served
  by `rules.get_company_info` so the agent quotes them rather than improvising credentials.

## Assertions can be scoped to one agent

An intake scenario should not fail because a downstream agent did its own job after the
handoff. Prefix a tool with an agent name to pin the expectation:

```yaml
must_not_call:
  - intake:calendar.create_appointment   # intake must not book; small_job may
  - crm.create_customer                  # unscoped: nobody may
```

## What production actually runs

**A customer talks to `flow/`, not to `agents/`.** The five agents below are the older
shape and are still what the `testkit` suite exercises; the live path was rewritten as one
agent walking a graph, and everything a real customer touches goes through it.

```
flow/flow.yaml        the graph: seventeen nodes, each with its own rules and its own tools
flow/rules/           one file per node's instructions, assembled into its prompt
flow/sim/tools.py     the tools, written once and run against either world
flow/world.py         the words both worlds share, and the one exception either can raise
flow/sim/world.py       everything in memory — what the scenarios run against
flow/live/world.py      sqlite plus the real services — what a customer runs against
flow/runner/engine.py one conversation walking the graph
flow/runner/harness.py the scenario suite: `python3 -m flow.runner.harness --repeat 3`
```

The two worlds are the point. A node prompt is tested against the simulator and then put in
front of a person unchanged, because the tools, the schemas and the wording are the same
object — only what is underneath differs. Anything that reaches outside the process
(`send_sms`, `send_email`, `notify_technician`, `escalate`, `schedule_followup`) is a
method on the world, gated by `PLUMBING_LIVE_*`, and raises rather than failing quietly:
a text that will not send stops the agent telling somebody they are booked.

`python3 scripts/check_live.py --all` fires one harmless call down every outbound leg and
says which of them can reach its service. Run it before a real customer.

## Layout

```
config/     business rules, state machine, agent registry, tool catalogue, model config, world seed
flow/       the graph a customer actually talks to — see above
agents/     each agent's prompt for the older five-agent shape; still driven by testkit
personas/   the AI-played customer, technician and supervisor
scenarios/  testkit scenarios; flow/scenarios/ holds the graph's own
src/plumbing/
  world.py        simulated world: virtual clock, CRM, calendar, payments, outboxes, hard gates
  store.py        sqlite: customers, tickets, appointments, follow-ups, messages, events
  tools/          simulated tools, signatures matching the real systems
  agent.py        the generic agent loop (assemble prompt + tools + tool calling)
  orchestrator.py conversation orchestration and agent handoff
  integrations/   the real adapters — Twilio, Telegram, Gmail, Google Calendar, Stripe
  live/           production: the HTTP surface, sessions, job offers, follow-up reminders
  sim/            the three human simulators
  testkit/        runner / assertions / judge / doctor / loop
  livestatus.py   writes "who is working" to runs/live.json for the console
  dashboard/      the local console (stdlib http.server + a single-page app)
runs/           per-run transcripts, tool logs and reports
prompt_history/ a snapshot and reason for every prompt change, manual or automatic
```

## Three design decisions that carry the weight

**1. Virtual clock.** The emergency flow needs "one calling round every 10 minutes, six
rounds, one hour". Waiting an actual hour is not viable. `clock.now/advance` run through
the simulated world, and a scenario can start on a Sunday, at 18:30, or on a public
holiday — landing directly on the rate bands and the "no standard bookings on Sundays" branch.

**2. Hard gates turn prohibitions into executable constraints.** No dispatch before the
deposit clears, no automatic refund once the technician has departed, no standard booking on
a closed day, no skipping ticket states. All enforced in the tool layer: the call is refused,
the reason goes back to the agent so it can correct itself, and a violation is recorded so
the assertions fail.

**3. The humans are LLMs, not scripted playback.** The customer gets a personality and a
bottom line from the scenario and words it differently every run. The technician's
*outcome* (accept / decline / no answer) is fixed deterministically by the scenario while
the *words* are generated — assertions hold, and the conversation is still real.

## How the healing loop avoids cheating

Doctor may only edit Markdown under `agents/`:

- Not Python — that would bypass the hard gates.
- Not `business_rules.yaml` — rewriting the rules to pass a test is tampering.
- Not `scenarios/` — rewriting the exam to pass it.

With the `claude_cli` backend the model edits files directly with permission checks
bypassed, so guards are enforced afterwards: every protected file is hashed before and
after, and the whole attempt is discarded and reverted if anything outside `agents/` moved.

The cycle: edit → re-run the scenario → if it passes, **run full regression** → if
regression broke something, revert automatically and try a different approach → at most N
rounds per scenario → otherwise report for a human. Every change leaves a snapshot and a
reason in `prompt_history/`.

Regression is the step that matters. Doctor will happily hard-code a rule to fix scenario A
and break scenario B doing it.

## Worth knowing

The customer simulator runs at temperature 0.9, so the same scenario never produces the same
conversation twice. That is deliberate — what is being tested is whether the agent handles a
real, off-script person, not whether it can replay a transcript. The cost is occasional
flake: one failure is not proof a prompt is wrong; two in a row is worth acting on.

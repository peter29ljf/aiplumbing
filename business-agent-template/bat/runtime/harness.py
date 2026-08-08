"""Run scenarios against the flow, several at once, and report every failure together.

One at a time meant one fix per run, and a run is minutes. Everything here is written to
collect: a scenario that fails keeps going where it can, the report lists all of them, and
the fixing is one pass rather than six.

    python3 -m bat.runtime.harness --project plumbing
    python3 -m bat.runtime.harness --project plumbing apartment   # ids containing this
    python3 -m bat.runtime.harness --project plumbing node_ --repeat 4
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bat.runtime import project as projects
from bat.runtime import registry as sim_tools
from bat.runtime.engine import Conversation
from bat.runtime.graph import load
from bat.runtime.sim import World

# The company's own name goes in from business_rules.yaml. It used to say "a plumbing
# company" to every simulated customer of every project, which is a small thing until a
# dental practice's test suite reads like a wrong number.
CUSTOMER_BRIEF = """You are a member of the public messaging %(company)s. Stay in
character and answer as that person would: short messages, plain words, one or two
sentences. You are not an assistant and you never help the company do its job — if they
ask something you would not know, say so.

Keep going until one of these has actually happened, then reply with exactly DONE:

  - you have been given a date and time and told the visit is booked
  - you have been told plainly that they cannot do this work at all
  - you have been told a technician will look at it and come back to you about a price

Nothing else ends it. "Let me take it from here", "I've noted that", "one moment" and
"somebody will help you shortly" are not endings — they mean carry on, so answer whatever
they last asked or press for what you came for.

%(persona)s"""


# Turned on per run, never by default, and that is the whole point of it.
#
# A cooperative simulation is a poor proxy for a person: RealUserSim (arXiv 2605.20204)
# measured unconstrained LLM users matching real style 6–8% of the time, and grounding
# them dropped tau-bench success by 3.5% on average while exposing three classes of
# failure the polite version never reached. Simulated Customers Never Walk Away
# (arXiv 2606.20708) is the other half: they do not get bored, do not go quiet, and do not
# leave.
#
# Left on by default this would silently move every score at once and the old numbers
# would stop meaning anything. Run the suite twice instead — the gap between clean and
# awkward is the measurement, and it is an estimate of how much the clean number flatters
# the agent.
AWKWARD = """

You are also a real person having a bad day, and you behave like one:

  - **Type like someone on a phone.** Lower case, no apologies for it, the occasional
    typo you do not correct, "thx", "pls", messages sent in two halves.
  - **Ask more than one thing at once**, and sometimes ask something that belongs three
    steps later — the price, the parking, whether they take cash.
  - **Change your mind once.** Give a date and then say actually that week is no good.
  - **Contradict yourself once**, mildly, the way people do, and correct it only if they
    notice.
  - **Get short with them if they ask you the same thing twice**, because you already
    said it and they were not listening.

None of this makes you unreasonable. You still want the thing you came for, you still
answer direct questions, and DONE still means what it meant. You are simply not the
unnaturally patient person who reads every word and answers exactly one question at a
time — that person is not who rings a plumber."""


@dataclass
class Result:
    id: str
    passed: bool = True
    problems: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    turns: int = 0
    budget: int = 0
    seconds: float = 0.0
    transcript: list[tuple[str, str]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    steps: list[Any] = field(default_factory=list)
    verdicts: list[Any] = field(default_factory=list)
    # Why the loop ended. "budget" and "customer left" look identical from the outside and
    # have opposite fixes — one is a scenario that needed more room, the other a simulated
    # customer that walked out on a mid-flow pleasantry. Every dental failure was reported
    # as the first and every one of them was the second.
    stopped: str = "budget"
    smells: list[Any] = field(default_factory=list)

    def wrong(self, problem: str) -> None:
        self.passed = False
        self.problems.append(problem)


def run_one(path: Path, llm_factory, project: projects.Project, *,
            awkward: bool = False) -> Result:
    spec = yaml.safe_load(path.read_text())
    result = Result(id=spec["id"])
    expect = spec.get("expect") or {}

    rules = project.business_rules()
    world = World(now=spec["now"], seed=spec.get("seed"), rules=rules,
                  records=project.records())
    llm = llm_factory()
    # `start:` drops the conversation in part-way down the graph with the ticket already
    # carrying what the earlier steps would have written. A node that has failed four days
    # running should not cost twenty model calls to reach, nineteen of which exercise
    # nodes nobody is worried about.
    start = spec.get("start") or {}
    talk = Conversation(world, llm,
                        load(project, known_tools=sim_tools.load_tools(project)),
                        start_at=start.get("node", ""), known=start.get("known"))

    customer = spec["customer"]
    # The number is a field on the scenario and was never handed to the persona, so the
    # simulated customer truthfully said they did not have one — and the agent, correctly,
    # refused to book. Three scenarios failed on a fact the test never told anybody.
    persona = customer["persona"].strip()
    if customer.get("phone"):
        persona += f"\n\nYour phone number is {customer['phone']}. Give it when asked."
    company = (rules.get("company") or {}).get("name") or "a local business"
    brief = CUSTOMER_BRIEF % {"company": company, "persona": persona}
    history = [{"role": "system", "content": brief + (AWKWARD if awkward else "")}]
    # "hi" from the top; a node scenario starts mid-conversation and opens with whatever
    # the customer would actually be saying at that point.
    said = str(customer.get("opens") or "hi")
    # What they say when they come back after it was all settled. Scripted rather than
    # left to the persona, because the persona has been told to stop at DONE — and the
    # engine's `_start_again` had never once been exercised.
    comes_back = str(customer.get("comes_back") or "")
    began = time.monotonic()

    # The scenario's own number if it names one; otherwise from the graph.
    budget = int(customer.get("max_turns") or turn_budget(talk.flow))
    result.budget = budget
    footprints: list[tuple] = []

    for _ in range(budget):
        result.turns += 1
        result.transcript.append(("customer", said))
        try:
            turn = talk.say(said)
        except Exception as exc:  # noqa: BLE001 - one scenario must not stop the rest
            result.wrong(f"the conversation raised {type(exc).__name__}: {exc}")
            break
        result.nodes.extend(n for n in turn.nodes if n not in result.nodes)
        result.steps.extend(turn.steps)
        result.transcript.append(("agent", turn.reply))

        footprints.append(_footprint(turn, world))
        if len(footprints) >= STALLED_AFTER and len(set(footprints[-STALLED_AFTER:])) == 1:
            result.wrong(f"went round in circles: {STALLED_AFTER} turns running the same "
                         f"tools with nothing changing")
            result.stopped = "stalled"
            break

        if talk.finished:
            if not comes_back:
                result.stopped = "the flow ended"
                break
            history.append({"role": "user", "content": turn.reply or "(said nothing)"})
            said, comes_back = comes_back, ""
            history.append({"role": "assistant", "content": said})
            # The persona has already replied DONE once, for the job that is settled, and
            # left to itself it says it again on its next breath — so the second
            # conversation got two turns and no appointment. Re-arm it against the new job.
            history.append({"role": "system", "content":
                "That first job is settled and you have moved on. What you have just "
                "raised is a separate job and none of it is arranged yet. Do not reply "
                "DONE again until this new one has been booked, refused, or handed to a "
                "technician on its own account."})
            continue

        history.append({"role": "user", "content": turn.reply or "(said nothing)"})
        reply = llm.chat("customer", history)
        said = (reply.content or "").strip()
        history.append({"role": "assistant", "content": said})
        if said.upper().startswith("DONE"):
            # Checked against the world, not against what they were told. The brief ends
            # the conversation on wording — "a technician will come back to you about a
            # price" — and that sentence is a mid-flow pleasantry in half these graphs. A
            # simulated customer who leaves on it produces a scenario that failed for
            # having been abandoned, reported as the agent never finishing.
            if _anything_happened(world):
                result.stopped = "the customer left"
                break
            said = ("Sorry — nothing has actually been arranged yet as far as I can "
                    "tell. What happens next?")
            history.append({"role": "assistant", "content": said})

    # This scenario is over, so its process is too. Closing only at the end of the suite
    # bounds nothing: fifty-four scenarios at ten workers still ends with fifty-four live
    # `claude` processes, because the tenth one starts long before the first is collected.
    # Closed here, the number alive is the number running.
    if hasattr(talk, "close"):
        try:
            talk.close()
        except Exception:  # noqa: BLE001 - a stuck process must not lose the result
            pass

    _afterwards(spec, world)
    result.seconds = round(time.monotonic() - began, 1)
    result.snapshot = world.snapshot()
    result.snapshot.setdefault("ended", world.ended)
    result.usage = llm.usage.as_dict() if hasattr(llm, "usage") else {}
    _judge(result, expect, talk, world)
    from bat.runtime.smells import sniff

    result.smells = sniff(result)
    if not result.passed:
        from bat.runtime.diagnose import diagnose

        result.verdicts = diagnose(result, talk.flow)
    return result


def _longest_path(flow) -> int:
    """Nodes on the longest way from the entry to an ending.

    Depth-first with a visited set, so a graph that loops back (test -> iterate -> test)
    counts each node once rather than running forever.
    """
    def walk(name: str, seen: frozenset[str]) -> int:
        if name in seen or name not in flow.nodes:
            return 0
        seen = seen | {name}
        onward = [walk(target, seen) for target in flow.nodes[name].exits]
        return 1 + max(onward, default=0)

    return walk(flow.entry, frozenset())


def turn_budget(flow) -> int:
    """How many customer turns a scenario gets, from the shape of the graph.

    A ten-node reference project's number was being copied into eighteen-node graphs, and
    a quarter to a half of all failures were conversations still moving when the turns ran
    out — reported as the agent never finishing.

    Two turns per node on the longest path, plus six. The multiplier is an engineering
    estimate, not a result: there is no established way to derive this. What matters is
    not that the number is right but that running out of budget is told apart from getting
    something wrong, which is what the fault classification is for.
    """
    return _longest_path(flow) * 2 + 6


# Three turns where the same tool ran and the world did not move. Not stuck for one turn —
# a step legitimately asks a question and waits. Three is a conversation going in circles;
# one production system repeated the same answer fifty-eight times.
STALLED_AFTER = 3


def _footprint(turn: Any, world: World) -> tuple:
    """What this turn did, for telling "going round in circles" from "still working"."""
    tools = tuple(sorted(tool for step in turn.steps for tool in step.tools))
    snapshot = world.snapshot()
    return (tools, tuple(len(snapshot.get(key) or ())
                         for key in ("appointments", "texts", "emails",
                                     "technician_messages", "escalations", "followups")))


def _anything_happened(world: World) -> bool:
    """Did this conversation leave a mark anybody could act on?

    A booking, a message, a handover, or a ticket that moved. Deliberately generous — the
    question is "was this conversation real", not "was it right"; whether it was right is
    what `expect` is for.
    """
    snapshot = world.snapshot()
    if any(snapshot.get(key) for key in
           ("appointments", "texts", "emails", "technician_messages", "escalations")):
        return True
    return any(ticket.get("history") for ticket in (snapshot.get("tickets") or {}).values())


def _lines_from(result: Result, node_name: str) -> list[str]:
    """What one node said. The agent's replies line up, in order, with the steps that
    spoke — the same pairing `diagnose._spoke_out_of_turn` relies on."""
    spoke = [step.node for step in result.steps if step.said]
    lines = [text for who, text in result.transcript if who == "agent" and text]
    return [line for node, line in zip(spoke, lines) if node == node_name]


def _afterwards(spec: dict, world: World) -> None:
    """The days after the conversation, if the scenario asks for them.

    The follow-up loop is where a ticket actually closes, and it runs on a clock nobody
    was winding. Each step is either time passing — which makes an ask fall due — or the
    technician finally saying something back.

        after:
          - { hours: 24 }            # he is asked
          - { hours: 24 }            # no answer, asked again
          - { technician: done }     # closes the ticket, thanks the customer
    """
    from datetime import timedelta

    from bat.runtime import followup

    for step in spec.get("after") or []:
        if "hours" in step:
            world.now += timedelta(hours=int(step["hours"]))
            followup.tick(world)
        if "technician" in step:
            done = str(step["technician"]).lower() in ("done", "yes", "finished", "true")
            for record in [f for f in world.followups if not f.get("answered")]:
                followup.technician_says(world, record["ticket_id"], done=done)


# Every assertion `_judge` understands by name. Anything else has to be a countable list
# in the world snapshot, or it is reported as unchecked rather than passed over.
HANDLED = frozenset({
    "reaches", "not_reaches", "ticket_status", "ticket_status_any", "tickets",
    "followup_asks",
    "must_say", "must_say_any", "must_not_say", "must_not_say_in", "finishes",
    "ticket_tags", "asks_every_time",
})


def _judge(result: Result, expect: dict, talk: Conversation, world: World) -> None:
    if (wanted := expect.get("reaches")) and wanted not in result.nodes:
        result.wrong(f"never reached `{wanted}` — went {' → '.join(result.nodes)}")

    if (forbidden := expect.get("not_reaches")) and forbidden in result.nodes:
        result.wrong(f"reached `{forbidden}`, which it should not have")

    if wanted := expect.get("ticket_status"):
        got = next((t["status"] for t in result.snapshot["tickets"].values()), "(no ticket)")
        if got != wanted:
            result.wrong(f"ticket ended `{got}`, expected `{wanted}`")

    # Where a conversation ends can depend on something outside the agent's control — an
    # emergency ends dispatched if somebody took the job and refunded if nobody did, and
    # both are the step doing its work correctly.
    if (options := expect.get("ticket_status_any")):
        got = next((t["status"] for t in result.snapshot["tickets"].values()), "(none)")
        if got not in options:
            result.wrong(f"ticket ended `{got}`, expected one of {options}")

    if (wanted := expect.get("tickets")) is not None:
        got = len(result.snapshot["tickets"])
        if got != wanted:
            result.wrong(f"{got} ticket(s), expected {wanted}")

    if (wanted := expect.get("followup_asks")) is not None:
        got = sum(1 for m in result.snapshot["technician_messages"]
                  if m.get("kind") == "followup")
        if got != wanted:
            result.wrong(f"the technician was chased {got} time(s), expected {wanted}")

    # Anything in the snapshot that is a list can be counted, which is how a project's
    # own nouns work: travel's tools record `enquiries`, a takeaway's record `orders`, and
    # a scenario asserts on them without the kit having to know the word.
    for key, value in expect.items():
        if key in HANDLED or not isinstance(value, int):
            continue
        entries = result.snapshot.get(key)
        if not isinstance(entries, list):
            continue
        if key == "technician_messages":
            # Being told about a job and being chased about it afterwards are two
            # different things; counting them together would make this number mean
            # nothing the moment a scenario runs the follow-up clock.
            entries = [m for m in entries if m.get("kind") != "followup"]
        if len(entries) != value:
            result.wrong(f"{len(entries)} {key.rstrip('s')}(s), expected {value}")

    # A field the conversation was supposed to leave on the ticket.
    for key, wanted in (expect.get("ticket_tags") or {}).items():
        got = next((t["tags"].get(key) for t in result.snapshot["tickets"].values()), None)
        if str(got) != str(wanted):
            result.wrong(f"ticket tag `{key}` is {got!r}, expected {wanted!r}")

    spoken = " ".join(text for who, text in result.transcript if who == "agent").lower()
    for phrase in expect.get("must_say", []):
        if phrase.lower() not in spoken:
            result.wrong(f"never said {phrase!r}")
    # Any one of these will do. `must_say` requires every phrase, which is right for a
    # figure and wrong for a wording: a scenario demanded "starting at" and the agent
    # wrote "a corporate year-end starts at CAD 1,800" — the same promise, in the same
    # breath, failed on a suffix.
    if (options := expect.get("must_say_any")):
        if not any(phrase.lower() in spoken for phrase in options):
            result.wrong(f"said none of {options}")

    for phrase in expect.get("must_not_say", []):
        if phrase.lower() in spoken:
            result.wrong(f"said {phrase!r}, which it must not")

    # Per node, because a phrase can be a lie from one step and the plain truth from the
    # next. "You're all set" out of `offer_options` is a booking that has not happened;
    # out of `booking`, which has just made one, it is what the customer should hear. The
    # whole-transcript check called the second a failure of the first.
    for node_name, phrases in (expect.get("must_not_say_in") or {}).items():
        said_there = " ".join(_lines_from(result, node_name)).lower()
        for phrase in phrases:
            if phrase.lower() in said_there:
                result.wrong(f"`{node_name}` said {phrase!r}, which it must not")

    # Every message except the last hands the conversation back. One with nothing to
    # answer leaves the customer guessing, and they guess "wait".
    #
    # This was a smell for a day, which means it failed nothing: a reference project
    # scored fifteen out of fifteen while a real person, testing it by hand, typed "?" six
    # times — once for each "Thanks, I have that number." A check that cannot fail is a
    # note in the margin.
    #
    # `dead_ends` in harness.yaml sets how many are tolerated; 0 is the default and the
    # honest number, because one is already a customer sitting in silence.
    if expect.get("asks_every_time", True):
        allowed = int(world.rules.get("_dead_ends_allowed", 0)) if world else 0
        from bat.runtime.smells import _asks_for_something

        spoken = [text for who, text in result.transcript
                  if who == "agent" and (text or "").strip()]
        dead = [t for t in spoken[:-1] if not _asks_for_something(t)]
        if len(dead) > allowed:
            result.wrong(
                f"{len(dead)} of {max(len(spoken) - 1, 0)} messages gave them nothing to "
                f"answer — first was {dead[0][:60]!r}")

    # A node scenario stops at a node in the middle of the graph, which is not an ending
    # and must not be reported as a failure to reach one.
    if not talk.finished and expect.get("reaches") and expect.get("finishes", True):
        result.wrong("the conversation never finished")

    # An assertion nobody checks is worse than no assertion: it reads as coverage and
    # provides none. Thirty-seven of them were sitting across three projects — every
    # travel scenario asserting `enquiries: 1` on a tool that recorded nothing, a suite
    # scoring 13/15 with the central action of the business never once performed. Now a
    # key nothing above understood stops the scenario.
    unchecked = [
        key for key, value in expect.items()
        if key not in HANDLED
        and not (isinstance(value, int) and isinstance(result.snapshot.get(key), list))
    ]
    if unchecked:
        result.wrong(
            f"nothing checks {', '.join(sorted(unchecked))} — either the tools never "
            f"recorded it in the world, or the key is a typo. Countable right now: "
            f"{', '.join(sorted(k for k, v in result.snapshot.items() if isinstance(v, list)))}"
        )


def _write_report(results: list[Result], runs: Path) -> Path:
    """Every exchange, every model call and every verdict, kept.

    The console prints a summary and a summary is not enough to argue with. Reading why
    something was called the model's fault means seeing what it was offered on that call,
    and that is gone the moment the process exits.
    """
    import json
    from datetime import datetime

    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps([
        {
            "id": r.id,
            "passed": r.passed,
            "problems": r.problems,
            "stopped": r.stopped,
            "verdicts": [{"source": v.source, "because": v.because, "where": v.where}
                         for v in r.verdicts],
            "nodes": r.nodes,
            "turns": r.turns,
            "seconds": r.seconds,
            "usage": r.usage,
            "smells": [{"kind": s.kind, "detail": s.detail} for s in r.smells],
            "transcript": [{"who": who, "text": text} for who, text in r.transcript],
            # `text` and `delta` too. A verdict that a step claimed something the world
            # does not bear out is exactly the kind somebody will want to check later, and
            # the report is the only place they can — the Step objects are gone the moment
            # the process exits. The report's whole reason for existing is that a summary
            # is not enough to argue with.
            "calls": [{"node": s.node, "seconds": s.seconds, "used": s.tools,
                       "offered": s.offered, "said": s.said, "refusals": s.refusals,
                       "text": getattr(s, "text", ""), "delta": getattr(s, "delta", {}),
                       "saw": getattr(s, "saw", "")[:2000],
                       "tokens_in": getattr(s, "tokens_in", 0),
                       "cached": getattr(s, "cached", 0)}
                      for s in r.steps],
            "snapshot": r.snapshot,
        }
        for r in results
    ], indent=2, default=str), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a project against its scenarios")
    parser.add_argument("--project", required=True,
                        help="a name under bat/projects/, or a path to one")
    parser.add_argument("only", nargs="?", default="", help="substring of a scenario id")
    # Ten, since two scenarios started going end to end. They are independent, so the
    # only cost of more is what the provider will take at once — and the first call of
    # every scenario landing together already shows as fifty seconds of contention in the
    # smallest node in the graph.
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--transcript", action="store_true", help="print every exchange")
    parser.add_argument("--engine", default="",
                        help="api (the default), claude-code, or claude-code-skills. "
                             "Overrides harness.yaml.")
    parser.add_argument("--awkward", action="store_true",
                        help="a customer who types badly, asks three things at once and "
                             "changes their mind — run it against the clean suite and "
                             "read the gap")
    # A verdict from one run has been wrong here before, and a whole day's failure list was
    # thrown away because of it. Repeats also fill the workers: six scenarios cannot use
    # ten of them, so nothing was ever actually running ten at a time.
    parser.add_argument("--repeat", type=int, default=1, help="run each scenario N times")
    args = parser.parse_args(argv)

    from bat.runtime.llm import LLM
    from bat.runtime.paths import load_dotenv

    load_dotenv()
    project = projects.find(args.project)
    settings = project.model()

    # Which engine drives the agent. The fix loop used to test one thing and ship
    # another: it always ran the API engine, whatever the project was actually deployed
    # on, so a score was a statement about a model nobody was using.
    engine = args.engine or (project.harness().get("engine") or "api")
    desk = server = None
    if engine != "api":
        from bat.engines import desk as tool_desk
        from bat.engines.per_node import Conversation as PerNode
        from bat.engines.per_skill import SkillConversation as PerSkill

        desk = tool_desk.Desk()
        endpoint, server = tool_desk.serve(desk)
        made = iter(range(100000))
        shape = PerSkill if engine.endswith("skills") else PerNode
        scratch = project.runs_dir.parent / "sessions"

        # Every conversation is a live `claude` process holding about 370 MB, and nothing
        # here closed them. Fifty-four scenarios means fifty-four processes: twenty
        # gigabytes wanted on a sixteen-gigabyte machine, five of swap in use, load
        # average forty-four, and after fifty minutes not one result. The run was not slow
        # — it was paging. `bridge/run.py` closed them in a `finally` and that did not
        # come across when the engine moved in here.
        live: list[Any] = []

        def _engine(world, llm, flow, *, start_at="", known=None):
            talk = shape(world, llm, flow, start_at=start_at, known=known, desk=desk,
                         name=f"conv{next(made)}", scratch=scratch)
            live.append(talk)
            return talk

        globals()["Conversation"] = _engine
        print(f"engine: {engine} · tools at {endpoint}")
    paths = sorted(p for p in project.scenarios_dir.glob("*.yaml") if args.only in p.stem)
    if not paths:
        print(f"No scenario matches {args.only!r}")
        return 2

    jobs = [p for p in paths for _ in range(args.repeat)]
    print(f"Running {len(paths)} scenario(s) x{args.repeat} = {len(jobs)} runs, "
          f"{args.workers} at a time ...\n", flush=True)
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(
            lambda p: run_one(p, lambda: LLM(settings), project, awkward=args.awkward),
            jobs))

    failed = [r for r in results if not r.passed]
    for result in results:
        mark = "ok  " if result.passed else "FAIL"
        print(f"  {mark} {result.id:<28} {result.turns:>2} turns  {result.seconds:>6.1f}s"
              f"  {' → '.join(result.nodes)}")
        for problem in result.problems:
            print(f"         - {problem}")
        for verdict in result.verdicts:
            print(f"         → {verdict}")
        for smell in result.smells:
            print(f"         ! {smell}")

    if args.repeat > 1:
        # Passing sometimes is not passing. A scenario that goes four for four is a
        # different thing from one that goes three, and only the second is worth a
        # morning — the first was never broken, it was unlucky.
        print("\nAcross repeats:")
        for name in sorted({r.id for r in results}):
            runs = [r for r in results if r.id == name]
            won = sum(1 for r in runs if r.passed)
            verdict = "PASS " if won == len(runs) else ("FLAKY" if won else "FAIL ")
            print(f"  {verdict} {name:<28} {won}/{len(runs)}")

    if args.transcript:
        for result in results:
            print(f"\n=== {result.id} ===")
            for who, text in result.transcript:
                print(f"  {who:<9}{(text or '')[:160]}")

    print(f"\n{len(results) - len(failed)}/{len(results)} passed "
          f"in {time.monotonic() - began:.0f}s wall clock")

    if server is not None:
        for talk in live:
            try:
                talk.close()
            except Exception:  # noqa: BLE001 - one stuck process must not hide the rest
                pass
        server.shutdown()

    from bat.runtime.assemble import overlong
    from bat.runtime.diagnose import summarise

    print(summarise(results))

    flow = load(project, known_tools=sim_tools.names())
    if (fat := overlong(flow)):
        print("\nPrompts that have grown past what a step can follow:")
        for line in fat:
            print(f"  {line}")
    print(f"\nfull record: {_write_report(results, project.runs_dir)}")

    # What it cost. The cache hit rate is the number to watch: a node's prompt is the same
    # every time it runs, so most of what goes up should be a repeat the provider already
    # has. A rate that falls means something is putting fresh text in front of the stable
    # part, which is the one way to make small prompts expensive again.
    totals = {}
    for result in results:
        for key, value in (result.usage or {}).items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
    if totals:
        prompt = totals.get("prompt_tokens", 0)
        hits = totals.get("cache_hit_tokens", 0)
        print(f"tokens: in {prompt:,} / out {totals.get('completion_tokens', 0):,}"
              f"   calls {totals.get('calls', 0):,}"
              + (f"   cache hits {hits:,} ({hits / prompt:.0%})" if prompt else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

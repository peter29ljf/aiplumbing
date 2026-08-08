"""Run scenarios against the flow, several at once, and report every failure together.

One at a time meant one fix per run, and a run is minutes. Everything here is written to
collect: a scenario that fails keeps going where it can, the report lists all of them, and
the fixing is one pass rather than six.

    python3 -m flow.runner.harness                 # all of them
    python3 -m flow.runner.harness apartment       # the ones whose id contains this
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from flow.runner.engine import Conversation  # noqa: E402
from flow.runner.graph import load  # noqa: E402
from flow.sim import tools as sim_tools  # noqa: E402
from flow.sim.world import World  # noqa: E402

SCENARIOS = ROOT / "flow" / "scenarios"

CUSTOMER_BRIEF = """You are a member of the public messaging a plumbing company. Stay in
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

%s"""


@dataclass
class Result:
    id: str
    passed: bool = True
    problems: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    turns: int = 0
    seconds: float = 0.0
    transcript: list[tuple[str, str]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    steps: list[Any] = field(default_factory=list)
    verdicts: list[Any] = field(default_factory=list)
    smells: list[Any] = field(default_factory=list)

    def wrong(self, problem: str) -> None:
        self.passed = False
        self.problems.append(problem)


def run_one(path: Path, llm_factory) -> Result:
    spec = yaml.safe_load(path.read_text())
    result = Result(id=spec["id"])
    expect = spec.get("expect") or {}

    world = World(now=spec["now"], seed=spec.get("seed"))
    llm = llm_factory()
    # `start:` drops the conversation in part-way down the graph with the ticket already
    # carrying what the earlier steps would have written. A node that has failed four days
    # running should not cost twenty model calls to reach, nineteen of which exercise
    # nodes nobody is worried about.
    start = spec.get("start") or {}
    talk = Conversation(world, llm, load(known_tools=sim_tools.names()),
                        start_at=start.get("node", ""), known=start.get("known"))

    customer = spec["customer"]
    # The number is a field on the scenario and was never handed to the persona, so the
    # simulated customer truthfully said they did not have one — and the agent, correctly,
    # refused to book. Three scenarios failed on a fact the test never told anybody.
    persona = customer["persona"].strip()
    if customer.get("phone"):
        persona += f"\n\nYour phone number is {customer['phone']}. Give it when asked."
    history = [{"role": "system", "content": CUSTOMER_BRIEF % persona}]
    # "hi" from the top; a node scenario starts mid-conversation and opens with whatever
    # the customer would actually be saying at that point.
    said = str(customer.get("opens") or "hi")
    # What they say when they come back after it was all settled. Scripted rather than
    # left to the persona, because the persona has been told to stop at DONE — and the
    # engine's `_start_again` had never once been exercised.
    comes_back = str(customer.get("comes_back") or "")
    began = time.monotonic()

    for _ in range(int(customer.get("max_turns", 12))):
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

        if talk.finished:
            if not comes_back:
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
            break

    _afterwards(spec, world)
    result.seconds = round(time.monotonic() - began, 1)
    result.snapshot = world.snapshot()
    result.snapshot.setdefault("ended", world.ended)
    result.usage = llm.usage.as_dict() if hasattr(llm, "usage") else {}
    _judge(result, expect, talk, world)
    from flow.runner.smells import sniff

    result.smells = sniff(result)
    if not result.passed:
        from flow.runner.diagnose import diagnose

        result.verdicts = diagnose(result, talk.flow)
    return result


def _lines_from(result: Result, node_name: str) -> list[str]:
    """What one node said, off the step that said it.

    It used to zip the steps that spoke against the agent's transcript lines and trust
    the two to stay in step. They did until a turn began joining a step's parting words
    to the next step's answer — one transcript line for two speaking steps, and every
    pairing after it silently wrong. The words live on the step now.
    """
    return [step.text for step in result.steps if step.node == node_name and step.text]


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

    from flow.runner import followup

    for step in spec.get("after") or []:
        if "hours" in step:
            world.now += timedelta(hours=int(step["hours"]))
            followup.tick(world)
        if "technician" in step:
            done = str(step["technician"]).lower() in ("done", "yes", "finished", "true")
            for record in [f for f in world.followups if not f.get("answered")]:
                followup.technician_says(world, record["ticket_id"], done=done)


def _judge(result: Result, expect: dict, talk: Conversation, world: World) -> None:
    if (wanted := expect.get("reaches")) and wanted not in result.nodes:
        result.wrong(f"never reached `{wanted}` — went {' → '.join(result.nodes)}")

    if (forbidden := expect.get("not_reaches")) and forbidden in result.nodes:
        result.wrong(f"reached `{forbidden}`, which it should not have")

    if wanted := expect.get("ticket_status"):
        got = next((t["status"] for t in result.snapshot["tickets"].values()), "(no ticket)")
        if got != wanted:
            result.wrong(f"ticket ended `{got}`, expected `{wanted}`")

    if (wanted := expect.get("tickets")) is not None:
        got = len(result.snapshot["tickets"])
        if got != wanted:
            result.wrong(f"{got} ticket(s), expected {wanted}")

    if (wanted := expect.get("followup_asks")) is not None:
        got = sum(1 for m in result.snapshot["technician_messages"]
                  if m.get("kind") == "followup")
        if got != wanted:
            result.wrong(f"the technician was chased {got} time(s), expected {wanted}")

    for key, label in (("appointments", "appointment"), ("texts", "text"),
                       ("emails", "email"),
                       ("technician_messages", "message to the technician"),
                       ("escalations", "escalation"), ("followups", "follow-up")):
        if key in expect:
            entries = result.snapshot[key]
            if key == "technician_messages":
                # Being told about a job and being chased about it afterwards are two
                # different things; counting them together would make this number mean
                # nothing the moment a scenario runs the follow-up clock.
                entries = [m for m in entries if m.get("kind") != "followup"]
            got = len(entries)
            if got != expect[key]:
                result.wrong(f"{got} {label}(s), expected {expect[key]}")

    spoken = " ".join(text for who, text in result.transcript if who == "agent").lower()
    for phrase in expect.get("must_say", []):
        if phrase.lower() not in spoken:
            result.wrong(f"never said {phrase!r}")
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

    # A node scenario stops at a node in the middle of the graph, which is not an ending
    # and must not be reported as a failure to reach one.
    if not talk.finished and expect.get("reaches") and expect.get("finishes", True):
        result.wrong("the conversation never finished")


def _write_report(results: list[Result]) -> Path:
    """Every exchange, every model call and every verdict, kept.

    The console prints a summary and a summary is not enough to argue with. Reading why
    something was called the model's fault means seeing what it was offered on that call,
    and that is gone the moment the process exits.
    """
    import json
    from datetime import datetime

    runs = ROOT / "flow" / "runs"
    runs.mkdir(exist_ok=True)
    path = runs / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps([
        {
            "id": r.id,
            "passed": r.passed,
            "problems": r.problems,
            "verdicts": [{"source": v.source, "because": v.because, "where": v.where}
                         for v in r.verdicts],
            "nodes": r.nodes,
            "turns": r.turns,
            "seconds": r.seconds,
            "usage": r.usage,
            "smells": [{"kind": s.kind, "detail": s.detail} for s in r.smells],
            "transcript": [{"who": who, "text": text} for who, text in r.transcript],
            "calls": [{"node": s.node, "seconds": s.seconds, "used": s.tools,
                       "offered": s.offered, "said": s.said, "refusals": s.refusals}
                      for s in r.steps],
            "snapshot": r.snapshot,
        }
        for r in results
    ], indent=2, default=str), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the flow against its scenarios")
    parser.add_argument("only", nargs="?", default="", help="substring of a scenario id")
    # Ten, since two scenarios started going end to end. They are independent, so the
    # only cost of more is what the provider will take at once — and the first call of
    # every scenario landing together already shows as fifty seconds of contention in the
    # smallest node in the graph.
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--transcript", action="store_true", help="print every exchange")
    # A verdict from one run has been wrong here before, and a whole day's failure list was
    # thrown away because of it. Repeats also fill the workers: six scenarios cannot use
    # ten of them, so nothing was ever actually running ten at a time.
    parser.add_argument("--repeat", type=int, default=1, help="run each scenario N times")
    args = parser.parse_args(argv)

    from plumbing.llm import LLM
    from plumbing.paths import load_dotenv

    load_dotenv()
    paths = sorted(p for p in SCENARIOS.glob("*.yaml") if args.only in p.stem)
    if not paths:
        print(f"No scenario matches {args.only!r}")
        return 2

    jobs = [p for p in paths for _ in range(args.repeat)]
    print(f"Running {len(paths)} scenario(s) x{args.repeat} = {len(jobs)} runs, "
          f"{args.workers} at a time ...\n", flush=True)
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda p: run_one(p, LLM), jobs))

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

    from flow.runner.diagnose import summarise

    print(summarise(results))
    print(f"\nfull record: {_write_report(results)}")

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

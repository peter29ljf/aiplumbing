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

    def wrong(self, problem: str) -> None:
        self.passed = False
        self.problems.append(problem)


def run_one(path: Path, llm_factory) -> Result:
    spec = yaml.safe_load(path.read_text())
    result = Result(id=spec["id"])
    expect = spec.get("expect") or {}

    world = World(now=spec["now"], seed=spec.get("seed"))
    llm = llm_factory()
    talk = Conversation(world, llm, load(known_tools=sim_tools.names()))

    customer = spec["customer"]
    history = [{"role": "system",
                "content": CUSTOMER_BRIEF % customer["persona"].strip()}]
    said = "hi"
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
        result.transcript.append(("agent", turn.reply))

        if talk.finished:
            break

        history.append({"role": "user", "content": turn.reply or "(said nothing)"})
        reply = llm.chat("customer", history)
        said = (reply.content or "").strip()
        history.append({"role": "assistant", "content": said})
        if said.upper().startswith("DONE"):
            break

    result.seconds = round(time.monotonic() - began, 1)
    result.snapshot = world.snapshot()
    result.usage = llm.usage.as_dict() if hasattr(llm, "usage") else {}
    _judge(result, expect, talk, world)
    return result


def _judge(result: Result, expect: dict, talk: Conversation, world: World) -> None:
    if (wanted := expect.get("reaches")) and wanted not in result.nodes:
        result.wrong(f"never reached `{wanted}` — went {' → '.join(result.nodes)}")

    if (forbidden := expect.get("not_reaches")) and forbidden in result.nodes:
        result.wrong(f"reached `{forbidden}`, which it should not have")

    if wanted := expect.get("ticket_status"):
        got = next((t["status"] for t in result.snapshot["tickets"].values()), "(no ticket)")
        if got != wanted:
            result.wrong(f"ticket ended `{got}`, expected `{wanted}`")

    for key, label in (("appointments", "appointment"), ("texts", "text"),
                       ("technician_messages", "message to the technician"),
                       ("escalations", "escalation"), ("followups", "follow-up")):
        if key in expect:
            got = len(result.snapshot[key])
            if got != expect[key]:
                result.wrong(f"{got} {label}(s), expected {expect[key]}")

    spoken = " ".join(text for who, text in result.transcript if who == "agent").lower()
    for phrase in expect.get("must_say", []):
        if phrase.lower() not in spoken:
            result.wrong(f"never said {phrase!r}")
    for phrase in expect.get("must_not_say", []):
        if phrase.lower() in spoken:
            result.wrong(f"said {phrase!r}, which it must not")

    if not talk.finished and expect.get("reaches"):
        result.wrong("the conversation never finished")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the flow against its scenarios")
    parser.add_argument("only", nargs="?", default="", help="substring of a scenario id")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--transcript", action="store_true", help="print every exchange")
    args = parser.parse_args(argv)

    from plumbing.llm import LLM
    from plumbing.paths import load_dotenv

    load_dotenv()
    paths = sorted(p for p in SCENARIOS.glob("*.yaml") if args.only in p.stem)
    if not paths:
        print(f"No scenario matches {args.only!r}")
        return 2

    print(f"Running {len(paths)} scenario(s), {args.workers} at a time ...\n", flush=True)
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda p: run_one(p, LLM), paths))

    failed = [r for r in results if not r.passed]
    for result in results:
        mark = "ok  " if result.passed else "FAIL"
        print(f"  {mark} {result.id:<28} {result.turns:>2} turns  {result.seconds:>6.1f}s"
              f"  {' → '.join(result.nodes)}")
        for problem in result.problems:
            print(f"         - {problem}")

    if args.transcript:
        for result in results:
            print(f"\n=== {result.id} ===")
            for who, text in result.transcript:
                print(f"  {who:<9}{(text or '')[:160]}")

    print(f"\n{len(results) - len(failed)}/{len(results)} passed "
          f"in {time.monotonic() - began:.0f}s wall clock")

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

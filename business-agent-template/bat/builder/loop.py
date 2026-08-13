"""Test, read the report, fix, test again — until it is usable or until it stops helping.

This is the part that runs unattended, so it is the part where the stopping rules matter
more than the fixing. Three of them:

- **usable** — the project's own `harness.yaml` says what that means. Reached, it stops.
- **flat** — two rounds running that bought nothing. Measured against the best score seen,
  not the last: a suite that goes 86 → 84 → 85 has improved twice by the wrong reading and
  not at all by the right one.
- **a question** — the fixer needs a decision that belongs to the owner. It stops and says
  what it needs, and nothing about "not stopping" is worth guessing a price for.

The loop never edits anything itself. It runs the harness, hands the report over verbatim,
and lets the builder decide — a summary of a failure is somebody's opinion about which
failure mattered.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from bat.builder import session
from bat.runtime import project as projects

ROOT = projects.PROJECTS.parents[1]

# The fixer says this when it wants a person. Matched on rather than inferred, so that
# "I could not decide" and "I have decided something questionable" do not look alike.
ASKING = re.compile(r"^\s*NEEDS A DECISION\b", re.MULTILINE | re.IGNORECASE)


def run_harness(name: str, *, repeat: int = 0, only: str = "",
                on_line: Callable[[str], None] | None = None) -> tuple[str, Path | None]:
    """Run the suite and give back everything it printed, plus the report it wrote."""
    project = projects.find(name)
    settings = project.harness()
    argv = [
        "python3", "-m", "bat.runtime.harness", "--project", name,
        "--repeat", str(repeat or settings.get("repeat", 4)),
        "--workers", str(settings.get("workers", 10)),
    ]
    if only:
        argv.append(only)

    lines: list[str] = []
    proc = subprocess.Popen(argv, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        lines.append(line)
        if on_line is not None:
            on_line(line)
    proc.wait()

    reports = sorted(project.runs_dir.glob("*.json"))
    return "\n".join(lines), (reports[-1] if reports else None)


def score(report: Path | None) -> tuple[float, int, bool]:
    """Pass rate, configuration faults, and whether every scenario went clean.

    Configuration faults are counted separately from everything else because they are the
    one kind that is ours: a rules file and a tool list contradicting each other. A build
    that ships with them has shipped a step that cannot do its job.
    """
    if report is None or not report.exists():
        return 0.0, 0, False

    runs = json.loads(report.read_text(encoding="utf-8"))
    if not runs:
        return 0.0, 0, False

    by_id: dict[str, list[bool]] = {}
    config_faults = 0
    for run in runs:
        by_id.setdefault(run["id"], []).append(bool(run["passed"]))
        config_faults += sum(1 for v in (run.get("verdicts") or [])
                             if v.get("source") == "config")

    won = sum(1 for run in runs if run["passed"])
    every_clean = all(all(results) for results in by_id.values())
    return won / len(runs), config_faults, every_clean


def once(name: str, *, on_line: Callable[[str], None] | None = None,
         stop: threading.Event | None = None) -> dict[str, Any]:
    """One round: run, score, and either stop or hand the report to the fixer."""
    build = session.load(name)
    rules = projects.find(name).harness()

    build.phase = session.TEST
    session.save(build)
    printed, report = run_harness(name, on_line=on_line)
    rate, config_faults, every_clean = score(report)

    session.note_round(build, rate, rules)
    outcome: dict[str, Any] = {
        "rate": rate, "config_faults": config_faults, "every_clean": every_clean,
        "round": build.rounds, "flat_rounds": build.flat_rounds,
        "report": report.name if report else "",
    }

    if session.usable(rate, config_faults, every_clean, rules):
        build.phase, build.waiting = session.DONE, ""
        build.note = (f"usable: {rate:.0%}, no configuration faults"
                      + (", every scenario clean" if every_clean else ""))
        session.save(build)
        return {**outcome, "stopped": "usable"}

    if session.give_up(build, rules):
        build.waiting = session.PAUSED
        build.note = (f"two rounds without improvement — best {build.best_score:.0%}. "
                      f"What is left is worth a person reading, not another round.")
        session.save(build)
        return {**outcome, "stopped": "flat"}

    if stop is not None and stop.is_set():
        build.waiting = session.PAUSED
        build.note = "paused between rounds"
        session.save(build)
        return {**outcome, "stopped": "asked"}

    build.phase = session.ITERATE
    session.save(build)
    reply = session.turn(build, FIX, report=printed, stop=stop)

    if ASKING.search(reply.text or ""):
        build.waiting = session.WAITING_FOR_ANSWER
        build.note = reply.text.strip()[:2000]
        session.save(build)
        return {**outcome, "stopped": "needs a decision", "asked": reply.text}

    return {**outcome, "stopped": "", "said": reply.text}


FIX = """Here is the run. Fix what it says, in one pass, then stop — the loop will
re-run the suite for you.

If any fix needs a decision that is the owner's — a price, a policy, whether to turn a
kind of job away, what to say when something is declined — do not invent one. Start your
reply with the line `NEEDS A DECISION` and say what you need. The loop stops there and
puts it to them."""


def until_done(name: str, *, limit: int = 8,
               on_line: Callable[[str], None] | None = None,
               stop: threading.Event | None = None) -> list[dict[str, Any]]:
    """Round after round until something says stop. `limit` is a backstop, not a plan."""
    history = []
    for _ in range(limit):
        outcome = once(name, on_line=on_line, stop=stop)
        history.append(outcome)
        if outcome["stopped"]:
            break
    return history


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test and fix until it is usable")
    parser.add_argument("project")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    # Flushed, because the whole point of printing during a twenty-minute run is being
    # able to see it happening. Buffered, the log file sat at zero bytes while the suite
    # ran, which is indistinguishable from a hang.
    def say(text: str) -> None:
        print(text, flush=True)

    for outcome in until_done(args.project, limit=args.limit, on_line=say):
        print(f"\n== round {outcome['round']}: {outcome['rate']:.0%}, "
              f"{outcome['config_faults']} config fault(s)"
              + (f" — stopped: {outcome['stopped']}" if outcome["stopped"] else ""),
              flush=True)
        if outcome.get("asked"):
            print(outcome["asked"], flush=True)

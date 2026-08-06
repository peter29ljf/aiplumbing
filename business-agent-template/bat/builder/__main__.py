"""Build an agent from the command line, when a browser is not what you want.

The console is the better way to watch one being built. This is for the other cases: a
brief you already wrote, three businesses you want going at once, or picking a build back
up after it stopped — which happens, and the two reasons it happens are worth naming.

    python3 -m bat.builder status
    python3 -m bat.builder new dental --brief ~/dental.md   # interview, then plan
    python3 -m bat.builder go dental                        # approve the plan and build
    python3 -m bat.builder test dental                      # run the harness
    python3 -m bat.builder fix dental                       # test, read, fix, repeat
    python3 -m bat.builder auto dental --brief ~/dental.md  # all of it, unattended

Every command is safe to run twice. The phase and the Claude Code session id live in the
project directory, so picking up after a laptop closed, a timeout fired, or an account ran
out of credit is the same command again — it resumes rather than starting over.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from bat.builder import claude, session
from bat.runtime import project as projects
from bat.runtime import registry
from bat.runtime.graph import BrokenFlow, load

ROOT = Path(__file__).resolve().parents[2]


def _say(text: str) -> None:
    print(text, flush=True)


def _turn(build: session.Build, said: str, *, report: str = "") -> claude.Reply:
    """One builder turn, with the tool calls shown as they happen."""
    def watch(event: dict) -> None:
        if event.get("type") != "assistant":
            return
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                what = (block.get("input") or {}).get("file_path") \
                    or (block.get("input") or {}).get("command") or ""
                _say(f"     {block['name']:<8} {str(what)[:90]}")

    reply = session.turn(build, said, report=report, on_event=watch)
    _say(f"   ${reply.spend.usd:.2f}  {reply.seconds:.0f}s  cache "
         f"{reply.spend.cache_hit_rate:.0%}")
    if not reply.ok:
        _say(f"   stopped: {build.note or reply.error}")
    return reply


def _loads(name: str) -> str:
    """The loader's verdict, which is also the compiler's. Empty means it is sound."""
    try:
        project = projects.find(name)
    except projects.NoSuchProject as missing:
        return str(missing)
    try:
        load(project, known_tools=registry.load_tools(project))
    except BrokenFlow as broken:
        return str(broken)
    return ""


def _harness(name: str, *, repeat: int = 0, only: str = "") -> tuple[str, float, int]:
    """Run it and hand back the output, the pass rate, and the configuration faults."""
    settings = projects.find(name).harness()
    argv = ["python3", "-m", "bat.runtime.harness", "--project", name,
            "--repeat", str(repeat or settings.get("repeat", 4)),
            "--workers", str(settings.get("workers", 10))]
    if only:
        argv.append(only)
    done = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True)
    output = done.stdout + done.stderr

    runs = sorted(projects.find(name).runs_dir.glob("*.json"))
    if not runs:
        return output, 0.0, 0
    latest = json.loads(runs[-1].read_text(encoding="utf-8"))
    won = sum(1 for run in latest if run["passed"])
    faults = sum(1 for run in latest for v in (run.get("verdicts") or [])
                 if v["source"] == "config")
    return output, (won / len(latest) if latest else 0.0), faults


# ----------------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> int:
    rows = []
    for directory in sorted(projects.PROJECTS.iterdir()):
        if not directory.is_dir():
            continue
        build = session.load(directory.name)
        spend, calls = claude.total(directory / "spend.jsonl")
        broken = _loads(directory.name)
        rows.append((directory.name, build.phase, build.waiting or "-",
                     f"${spend.usd:.2f}", calls, "yes" if not broken else "NO"))
    _say(f"{'project':<14}{'phase':<11}{'waiting':<16}{'spent':>8}{'calls':>7}{'loads':>7}")
    for row in rows:
        _say(f"{row[0]:<14}{row[1]:<11}{row[2]:<16}{row[3]:>8}{row[4]:>7}{row[5]:>7}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    brief = Path(args.brief).read_text(encoding="utf-8")
    build = session.start(args.name)
    for preset in ("model.yaml", "harness.yaml"):
        target = session.directory(args.name) / preset
        if not target.exists():
            target.write_text((projects.PRESETS / preset).read_text(encoding="utf-8"))

    _say(f"== {args.name}: interview")
    reply = _turn(build, brief + "\n\n" + (
        "Ask only what you genuinely cannot infer, and what a customer would actually "
        "hear. Do not ask me to read the brief back. Anything you can flag as an "
        "assumption instead, flag it and carry on."
        if args.unattended else
        "Ask me what you need."))
    if not reply.ok:
        return 1
    if not args.unattended:
        _say("\nAnswer it, then: python3 -m bat.builder plan " + args.name)
        return 0
    return cmd_plan(args)


def cmd_answer(args: argparse.Namespace) -> int:
    build = session.load(args.name)
    build.waiting, build.note = "", ""
    session.save(build)
    _say(f"== {args.name}: {build.phase}")
    return 0 if _turn(build, args.text).ok else 1


def cmd_plan(args: argparse.Namespace) -> int:
    build = session.load(args.name)
    if build.phase == session.INTERVIEW:
        session.advance(build)
    build.waiting, build.note = "", ""
    session.save(build)
    _say(f"== {args.name}: plan")
    reply = _turn(build, "Write PLAN.md. Anything still unsettled goes in the open "
                         "questions rather than being guessed at.")
    if reply.ok:
        _say(f"\nRead {session.directory(args.name) / 'PLAN.md'}, then: "
             f"python3 -m bat.builder go {args.name}")
    return 0 if reply.ok else 1


def cmd_go(args: argparse.Namespace) -> int:
    """Approve the plan and build. The one command that needs a person to have read
    something first, which is why it is not folded into `plan`."""
    build = session.load(args.name)
    if build.phase in (session.INTERVIEW, session.PLAN):
        session.advance(build)
    build.phase = session.BUILD
    build.waiting, build.note = "", ""
    session.save(build)

    _say(f"== {args.name}: build")
    broken = _loads(args.name)
    said = "Approved. Build it."
    if broken and "No project" not in broken:
        said = ("The build stopped part-way; what is there is on disk. The loader says:\n\n"
                + broken + "\n\nFinish it and get the loader clean.")
    reply = _turn(build, said + " Then run the loader and fix whatever it reports until "
                                "the graph loads clean.")
    if not reply.ok:
        return 1

    broken = _loads(args.name)
    _say(f"== {args.name}: {'loads clean' if not broken else 'still broken'}")
    if broken:
        _say(broken)
    return 0 if not broken else 1


def cmd_test(args: argparse.Namespace) -> int:
    output, rate, faults = _harness(args.name, repeat=args.repeat, only=args.only)
    _say(output[-4000:])
    _say(f"== {rate:.0%}, {faults} configuration fault(s)")
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    """Test, read the report, fix, repeat — until usable or until it stops improving.

    The stop conditions are the project's own, in `harness.yaml`. Two of them matter: no
    configuration faults, ever, and give up after two rounds that bought nothing —
    measured against the best score seen, because 86 then 84 then 85 is two rounds of
    nothing however the third looks against the second.
    """
    build = session.load(args.name)
    rules = projects.find(args.name).harness()

    for round_number in range(1, args.rounds + 1):
        output, rate, faults = _harness(args.name, repeat=args.repeat)
        session.note_round(build, rate, rules)
        every_clean = "FAIL " not in output and "FLAKY" not in output
        _say(f"== round {round_number}: {rate:.0%}, {faults} configuration fault(s)")

        if session.usable(rate, faults, every_clean, rules):
            build.phase = session.DONE
            session.save(build)
            _say(f"== {args.name} is usable")
            return 0
        if session.give_up(build, rules):
            _say(f"== stopping: {build.flat_rounds} rounds without improving on "
                 f"{build.best_score:.0%}. What is left needs a person to look.")
            return 1

        build.phase = session.ITERATE
        session.save(build)
        reply = _turn(build, "Fix what this run says. Change everything you are going to "
                             "change, then stop — the next run is mine to start.",
                      report=output[-14000:])
        if not reply.ok:
            return 1
    return 1


def cmd_auto(args: argparse.Namespace) -> int:
    args.unattended = True
    for step in (cmd_new, cmd_go, cmd_fix):
        if step(args) != 0:
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bat.builder", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="where every project has got to").set_defaults(
        run=cmd_status)

    for name, run, needs_brief in (("new", cmd_new, True), ("auto", cmd_auto, True)):
        one = sub.add_parser(name)
        one.add_argument("name")
        one.add_argument("--brief", required=needs_brief)
        one.add_argument("--unattended", action="store_true")
        one.add_argument("--repeat", type=int, default=0)
        one.add_argument("--rounds", type=int, default=4)
        one.add_argument("--only", default="")
        one.set_defaults(run=run)

    for name, run in (("plan", cmd_plan), ("go", cmd_go)):
        one = sub.add_parser(name)
        one.add_argument("name")
        one.set_defaults(run=run)

    answer = sub.add_parser("answer", help="reply to what it asked")
    answer.add_argument("name")
    answer.add_argument("text")
    answer.set_defaults(run=cmd_answer)

    test = sub.add_parser("test")
    test.add_argument("name")
    test.add_argument("--repeat", type=int, default=0)
    test.add_argument("--only", default="")
    test.set_defaults(run=cmd_test)

    fix = sub.add_parser("fix")
    fix.add_argument("name")
    fix.add_argument("--repeat", type=int, default=0)
    fix.add_argument("--rounds", type=int, default=4)
    fix.set_defaults(run=cmd_fix)

    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())

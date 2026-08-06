"""One project being built, and where it has got to.

Five phases, and the three ways a build stops. Everything durable lives in the project
directory — the phase, the Claude Code session id, the transcript, the ledger — so a
console restart, or a laptop closing, does not lose a build half way through an interview.

    interview  ask what cannot be inferred            stops on every question
    plan       write PLAN.md for a person to read     stops for approval
    build      write the files                        runs
    test       run the harness                        runs
    iterate    read the report and fix                stops when a decision is the owner's
    done       usable, by the project's own numbers

**Stopping is a first-class outcome, not a failure.** A generator that guesses a price to
avoid stopping produces an agent that quotes a price nobody agreed to.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bat.builder import claude
from bat.runtime.project import PRESETS, PROJECTS, Project

PROMPTS = Path(__file__).resolve().parent / "prompts"
TEMPLATE_ROOT = PRESETS.parent.parent

INTERVIEW, PLAN, BUILD, TEST, ITERATE, DONE = (
    "interview", "plan", "build", "test", "iterate", "done")
PHASES = (INTERVIEW, PLAN, BUILD, TEST, ITERATE, DONE)

# Why a build is not running. Empty means it is.
WAITING_FOR_ANSWER = "waiting for an answer"
WAITING_FOR_APPROVAL = "waiting for the plan to be approved"
PAUSED = "paused"
FAILED = "failed"


@dataclass
class Build:
    """The state of one project's construction. Serialised to `build.json`."""

    name: str
    phase: str = INTERVIEW
    waiting: str = ""
    session_id: str = ""
    rounds: int = 0                       # fix-and-retest rounds spent
    best_score: float = -1.0              # best pass rate seen, for the flat-round rule
    flat_rounds: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""                        # what it is waiting for, in words

    @property
    def running(self) -> bool:
        return not self.waiting and self.phase != DONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "phase": self.phase, "waiting": self.waiting,
            "session_id": self.session_id, "rounds": self.rounds,
            "best_score": self.best_score, "flat_rounds": self.flat_rounds,
            "note": self.note, "transcript": self.transcript,
        }


def directory(name: str) -> Path:
    return PROJECTS / name


def state_file(name: str) -> Path:
    return directory(name) / "build.json"


def load(name: str) -> Build:
    path = state_file(name)
    if not path.exists():
        return Build(name=name)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Build(**{k: v for k, v in raw.items() if k in Build.__annotations__})


def save(build: Build) -> None:
    path = state_file(build.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build.as_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8")


def start(name: str) -> Build:
    """Make the directory and the empty shape of a project. Nothing else.

    Deliberately not a scaffold full of TODO files: a half-written flow.yaml is something
    the builder has to read, understand and undo before it can write the real one.
    """
    project_dir = directory(name)
    for sub in ("rules", "tools", "scenarios", "runs"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)
    build = Build(name=name)
    save(build)
    return build


# ----------------------------------------------------------------------
def prompt_for(phase: str, said: str, *, project: Project | None = None,
               report: str = "") -> str:
    """What Claude Code is asked, for one turn of one phase.

    The architecture note goes in front of every phase and never changes, which is exactly
    what a prompt cache wants — it is the same bytes every call, and the provider stops
    charging full price for it after the first.
    """
    parts = [(PROMPTS / "architecture.md").read_text(encoding="utf-8").strip()]

    phase_file = PROMPTS / f"{phase}.md"
    if phase_file.exists():
        parts.append(phase_file.read_text(encoding="utf-8").strip())

    if project is not None:
        parts.append(
            f"# This project\n\n"
            f"Directory: `{project.dir}` (you are already in it)\n"
            f"Name for the harness: `{project.name}`\n\n"
            f"The tool kit available to every project is documented in "
            f"`{PRESETS / 'tools' / 'service.py'}`. Read it before naming a tool — a tool "
            f"that does not exist fails validation, and inventing one is slower than "
            f"looking."
        )

    if report:
        parts.append("# The run you are fixing from\n\n```\n" + report.strip() + "\n```")

    if said:
        parts.append("# What they said\n\n" + said.strip())

    return "\n\n---\n\n".join(parts)


def turn(build: Build, said: str, *, report: str = "", model: str = "",
         on_event: Callable[[dict[str, Any]], None] | None = None,
         stop: threading.Event | None = None) -> claude.Reply:
    """One exchange with Claude Code, recorded and billed to this project."""
    project_dir = directory(build.name)
    project_dir.mkdir(parents=True, exist_ok=True)

    project = Project(project_dir) if (project_dir / "flow.yaml").exists() else None
    if project is None:
        project = Project(project_dir)

    asked = prompt_for(build.phase, said, project=project, report=report)
    reply = claude.run(
        asked, project_dir=project_dir, session_id=build.session_id, model=model,
        # Read access to the kit: the preset tools and the reference project are what it
        # copies patterns from, and describing them in a prompt costs more than letting it
        # look. It is a sibling of the project directory, so this widens the write surface
        # too — worth knowing, and the reason nothing valuable lives there but templates.
        extra_dirs=(PRESETS,),
        on_event=on_event, stop=stop,
    )

    build.session_id = reply.session_id or build.session_id
    build.transcript.append({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "phase": build.phase,
        "said": said,
        "replied": reply.text,
        "usd": reply.spend.usd,
    })
    claude.record(reply, project_dir / "spend.jsonl", phase=build.phase, prompt=said)
    if not reply.ok:
        build.waiting = FAILED
        build.note = reply.error
    save(build)
    return reply


# ----------------------------------------------------------------------
def advance(build: Build) -> Build:
    """Move to the next phase, and say what it is now waiting for."""
    order = {INTERVIEW: PLAN, PLAN: BUILD, BUILD: TEST, TEST: ITERATE, ITERATE: TEST}
    build.phase = order.get(build.phase, DONE)
    build.waiting = WAITING_FOR_APPROVAL if build.phase == BUILD else ""
    if build.phase == BUILD:
        # The one hard stop in the middle. Everything after this writes files and spends
        # money, and the plan is the last point where that is cheap to redirect.
        build.note = "PLAN.md is written. Read it and approve before anything is built."
    save(build)
    return build


def usable(score: float, config_faults: int, every_clean: bool,
           rules: dict[str, Any]) -> bool:
    """Whether this is finished, by the project's own numbers.

    `config_faults` is the one that does not bend. It counts contradictions between a
    rules file and a tool list — the generator's own bugs — and a build that ships with
    them has shipped a step that cannot do its job.
    """
    want = (rules or {}).get("usable") or {}
    if config_faults > int(want.get("config_faults", 0)):
        return False
    if want.get("every_scenario_clean", True) and every_clean:
        return True
    return score >= float(want.get("min_pass_rate", 0.95))


def note_round(build: Build, score: float, rules: dict[str, Any]) -> Build:
    """Record a test round and count the ones that went nowhere.

    Improvement is measured against the best seen, not the last: a suite that goes
    86 → 84 → 85 has not improved twice, however the second number looks against the first.
    """
    build.rounds += 1
    if score > build.best_score:
        build.best_score, build.flat_rounds = score, 0
    else:
        build.flat_rounds += 1
    save(build)
    return build


def give_up(build: Build, rules: dict[str, Any]) -> bool:
    """Stop fixing when two rounds running have bought nothing.

    Learned the hard way: a suite sat between 84% and 86% for three rounds while the
    failures moved from node to node, and the useful signal was where they moved to, not
    what the total did.
    """
    want = (rules or {}).get("usable") or {}
    return build.flat_rounds >= int(want.get("stop_after_flat_rounds", 2))

"""One generated agent, and where everything it owns lives.

The runtime used to be a plumbing company. Not by design — by three import lines and three
path constants pointing at one directory. `Project` is what replaced them: a directory,
and the handful of files that make it an agent rather than a folder.

    projects/<name>/
        flow.yaml            the graph
        always.md            the preamble every node carries
        rules/*.md           one file per node's instructions
        tools/*.py           tools this project wrote for itself, if any
        scenarios/*.yaml     what it is tested against
        business_rules.yaml  prices, hours, what we will not take on
        model.yaml           which provider answers, per role
        harness.yaml         how it is tested and what counts as usable
        runs/                every harness report, kept
        spend.jsonl          what the builder cost to make it

Nothing is copied into a project except its own data. Giving each one a copy of the engine
would mean N copies of every bug, each fixed separately or not at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECTS = Path(__file__).resolve().parents[1] / "projects"
PRESETS = Path(__file__).resolve().parents[1] / "presets"


class NoSuchProject(Exception):
    """Named a project that is not there. Says which ones are."""


@dataclass(frozen=True)
class Project:
    dir: Path

    # ---- where things are --------------------------------------------
    @property
    def name(self) -> str:
        return self.dir.name

    @property
    def flow_yaml(self) -> Path:
        return self.dir / "flow.yaml"

    @property
    def rules_dir(self) -> Path:
        return self.dir / "rules"

    @property
    def scenarios_dir(self) -> Path:
        return self.dir / "scenarios"

    @property
    def tools_dir(self) -> Path:
        return self.dir / "tools"

    @property
    def runs_dir(self) -> Path:
        return self.dir / "runs"

    # ---- what they say -----------------------------------------------
    def always(self) -> str:
        """The preamble every node carries. The project's own, or the kit's."""
        own = self.dir / "always.md"
        return (own if own.exists() else PRESETS / "always.md").read_text(
            encoding="utf-8").strip()

    def rules(self, name: str) -> str:
        return (self.rules_dir / f"{name}.md").read_text(encoding="utf-8").strip()

    # ---- what it costs and how it is judged ---------------------------
    def business_rules(self) -> dict[str, Any]:
        return _yaml(self.dir / "business_rules.yaml")

    def model(self) -> dict[str, Any]:
        """Providers and roles. Falls back to the kit's default so a half-built project
        still runs — a missing model.yaml should not look like a broken graph."""
        own = self.dir / "model.yaml"
        return _yaml(own if own.exists() else PRESETS / "model.yaml")

    def harness(self) -> dict[str, Any]:
        own = self.dir / "harness.yaml"
        return _yaml(own if own.exists() else PRESETS / "harness.yaml")


def find(name: str | Path) -> Project:
    """By name under `projects/`, or by path if you hand it one."""
    candidate = Path(name)
    if candidate.is_dir() and (candidate / "flow.yaml").exists():
        return Project(candidate.resolve())

    found = PROJECTS / str(name)
    if not (found / "flow.yaml").exists():
        known = sorted(p.name for p in PROJECTS.iterdir()
                       if (p / "flow.yaml").exists()) if PROJECTS.exists() else []
        raise NoSuchProject(
            f"No project '{name}' — looked for {found / 'flow.yaml'}. "
            f"There {'is' if len(known) == 1 else 'are'}: {known or 'none yet'}"
        )
    return Project(found.resolve())


def every() -> list[Project]:
    if not PROJECTS.exists():
        return []
    return [Project(p.resolve()) for p in sorted(PROJECTS.iterdir())
            if (p / "flow.yaml").exists()]


@lru_cache(maxsize=32)
def _yaml_cached(path: str, mtime: float) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _yaml(path: Path) -> dict[str, Any]:
    """Cached on the file's mtime, so the console can edit a file and the next read sees
    it. A cache keyed on the path alone means saving a rules change and testing the old
    one, which is a confusing half hour."""
    if not path.exists():
        return {}
    return _yaml_cached(str(path), path.stat().st_mtime)

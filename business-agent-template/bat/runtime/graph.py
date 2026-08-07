"""Load flow.yaml and refuse to load a broken one.

Every check here answers a mistake that is cheap to make and expensive to find later: a
node name typed one way in `branch` and another way in `nodes`, a rules file renamed but
not renamed everywhere, a tool that no longer exists. All of them look fine until a real
conversation walks into them, and then they look like the model behaving strangely.

This is not a business gate. It is the difference between a typo failing at import and a
typo failing in front of a customer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bat.runtime.project import Project, find


class BrokenFlow(Exception):
    """flow.yaml does not describe a graph anyone can walk."""


@dataclass(frozen=True)
class Node:
    # Where this node's rules live. Carried on the node rather than looked up from a
    # module constant, so two projects can be loaded in one process — which the console
    # does the moment somebody opens a second tab.
    project: Project
    name: str
    goal: str
    rules: tuple[str, ...]
    tools: tuple[str, ...]
    sets_status: str
    next: str | None = None
    branch: dict[str, str] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.next is None and not self.branch

    @property
    def exits(self) -> tuple[str, ...]:
        if self.next:
            return (self.next,)
        return tuple(self.branch.values())

    @property
    def choices(self) -> tuple[str, ...]:
        """The named ways out. Empty when there is only one, or none at all."""
        return tuple(self.branch)


@dataclass(frozen=True)
class Flow:
    project: Project
    entry: str
    nodes: dict[str, Node]

    def __getitem__(self, name: str) -> Node:
        try:
            return self.nodes[name]
        except KeyError:
            raise BrokenFlow(f"No node called '{name}'. There are: {sorted(self.nodes)}") from None


def load(project: Project | str | Path, *, known_tools: set[str] | None = None) -> Flow:
    """Read one project's graph and check it hangs together.

    `known_tools` is passed in rather than imported, so the graph can be validated against
    whichever tool set is in play — the simulator today, a live backend later — and so
    this module never has to import the tools it is checking.
    """
    if not isinstance(project, Project):
        project = find(project)
    raw = yaml.safe_load(project.flow_yaml.read_text(encoding="utf-8")) or {}

    entry = raw.get("entry")
    node_specs = raw.get("nodes") or {}
    if not entry:
        raise BrokenFlow("flow.yaml has no `entry`, so there is nowhere to start.")
    if not node_specs:
        # Say what was found instead. A generated project wrote every node at the top
        # level with no `nodes:` wrapper, and "flow.yaml has no `nodes`" reads like the
        # file is empty — so three builds went looking for the wrong thing entirely.
        looks_like = [key for key, value in raw.items()
                      if key != "entry" and isinstance(value, dict)
                      and ("goal" in value or "tools" in value)]
        if looks_like:
            raise BrokenFlow(
                f"flow.yaml has no `nodes:` key, but {len(looks_like)} top-level entries "
                f"look like nodes ({', '.join(looks_like[:4])}"
                f"{'...' if len(looks_like) > 4 else ''}). They all need to sit under a "
                f"single `nodes:` key, indented beneath it."
            )
        raise BrokenFlow("flow.yaml has no `nodes`.")

    nodes: dict[str, Node] = {}
    for name, spec in node_specs.items():
        spec = spec or {}
        # `next:` with nothing after it is null in YAML, and null means not set — not
        # "set to nothing". Reading the key's presence instead would make an empty line
        # collide with a branch and refuse a file that is perfectly clear to a reader.
        if spec.get("next") is not None and spec.get("branch"):
            raise BrokenFlow(
                f"Node '{name}' has both `next` and `branch`. One way out or several, "
                f"not both — otherwise which one wins is whatever the code happens to do."
            )
        nodes[name] = Node(
            project=project,
            name=name,
            goal=str(spec.get("goal", "")).strip(),
            rules=tuple(spec.get("rules") or ()),
            tools=tuple(spec.get("tools") or ()),
            sets_status=str(spec.get("sets_status", "")).strip(),
            next=spec.get("next"),
            branch=dict(spec.get("branch") or {}),
        )

    _check(project, entry, nodes, known_tools)
    return Flow(project=project, entry=entry, nodes=nodes)


def _check(project: Project, entry: str, nodes: dict[str, Node],
           known_tools: set[str] | None) -> None:
    problems: list[str] = []

    if entry not in nodes:
        problems.append(f"entry '{entry}' is not one of the nodes")

    rules_dir = project.rules_dir
    for node in nodes.values():
        if not node.goal:
            problems.append(f"{node.name}: no `goal`, so the prompt has nothing to open with")
        if not node.sets_status:
            problems.append(f"{node.name}: no `sets_status`")

        for target in node.exits:
            if target not in nodes:
                problems.append(f"{node.name} leads to '{target}', which does not exist")

        for rule in node.rules:
            if not (rules_dir / f"{rule}.md").exists():
                problems.append(f"{node.name} wants rules/{rule}.md, which is not there")

        if known_tools is not None:
            for tool in node.tools:
                if tool not in known_tools:
                    problems.append(f"{node.name} wants the tool '{tool}', which does not exist")

        # A node signals it is finished by writing `outcome` with ticket.set_fields. One
        # that cannot call it has no way to say so and the conversation stops there —
        # which reads, from outside, as the model refusing to move on.
        if not node.is_terminal and "step.finished" not in node.tools:
            problems.append(
                f"{node.name} has to move on but cannot call step.finished, so it has "
                f"no way to say it is done"
            )

        # Terminal nodes need no way to end. Being terminal is what ends them — the
        # engine closes the conversation on the reply. Asking the model to announce a
        # fact the graph already holds is one more thing it can forget, and it did.

    # If a tool is missing and a file in tools/ registered nothing, that is almost
    # certainly why. Attached here rather than raised at import time: "does not exist"
    # reads like a typo in flow.yaml and sends people to the wrong file entirely, and
    # raising instead stopped three projects that had a junk file they never used.
    if any("does not exist" in problem for problem in problems):
        from bat.runtime.registry import complaints

        problems.extend(complaints())

    # A node nobody can reach is either a typo in somebody's `branch` or a leftover. Both
    # are worth knowing about: it will never run, and it will be maintained forever.
    reached, queue = {entry}, [entry]
    while queue:
        current = queue.pop()
        if current not in nodes:
            continue
        for target in nodes[current].exits:
            if target not in reached:
                reached.add(target)
                queue.append(target)
    for orphan in sorted(set(nodes) - reached):
        problems.append(f"nothing leads to '{orphan}' — it can never run")

    if problems:
        raise BrokenFlow("flow.yaml does not hang together:\n  - " + "\n  - ".join(problems))

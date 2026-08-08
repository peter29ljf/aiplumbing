"""A tool registered to the wrong function.

`@tool(...)` decorates whatever comes next. Put a helper between the decorator and the
function it was written for, and the decorator silently takes the helper instead — the
tool is registered, the loader is happy, the graph validates, and the only sign is at
runtime, in the middle of a customer conversation.

That is what happened. A one-line formatter was inserted above `rules_get_hours`, so
`rules.get_hours` became a function that takes a time string and was handed the world:

    Wrong arguments for rules.get_hours: strptime() argument 1 must be str, not World

Three suite runs went by. Every one of them the agent told the customer, honestly, that
the lookup was returning an error — and the wording was written up as the agent inventing
an excuse, and a rule was added scolding it for something it had not done. The tool was
broken, the agent was right, and the instrument blamed it for saying so.

Every tool's first argument is the world. Nothing else can be.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import project as projects  # noqa: E402
from bat.runtime import registry  # noqa: E402

PROJECTS = sorted(p.name for p in (ROOT / "bat" / "projects").iterdir()
                  if (p / "flow.yaml").exists())


@pytest.mark.parametrize("name", PROJECTS)
def test_every_tool_is_wired_to_a_function_that_takes_the_world(name):
    registry.load_tools(projects.find(name))

    wrong = []
    for tool, spec in registry._TOOLS.items():
        first = next(iter(inspect.signature(spec["handler"]).parameters), None)
        if first != "world":
            wrong.append(f"{tool} -> {spec['handler'].__name__}({first})")

    assert not wrong, ("a decorator took the wrong function: " + ", ".join(wrong))


@pytest.mark.parametrize("name", PROJECTS)
def test_every_tool_answers_something_when_called_with_nothing_missing(name):
    """A cheap smoke test for the tools that need no arguments. It would have caught this
    the moment it was introduced, in under a second, without a model."""
    project = projects.find(name)
    registry.load_tools(project)
    from bat.runtime.sim import World

    world = World(now="2026-08-07T12:00:00-07:00", rules=project.business_rules(),
                  records=project.records())
    # Only the tools some node in this project's graph actually lists.
    #
    # Two reasons, and both are worth knowing. `registry._TOOLS` is module-level and
    # accumulates across projects in one process — not a test artefact, the console does
    # it the moment somebody opens a second tab. And a kit tool reads the shape of
    # `business_rules.yaml` it was written for, so calling a plumber's pricing tool
    # against a restaurant's rules raises whether or not the restaurant ever grants it.
    from bat.runtime.graph import load

    flow = load(project, known_tools=registry.names())
    reachable = {t for node in flow.nodes.values() for t in node.tools}

    broke = []
    for tool, spec in registry._TOOLS.items():
        if tool not in reachable:
            continue
        wants = spec["schema"]["function"]["parameters"].get("required") or []
        if wants:
            continue                      # needs arguments only a conversation can supply
        result, _ = registry.call(world, spec["schema"]["function"]["name"], "{}", (tool,))
        if isinstance(result, dict) and "Wrong arguments" in str(result.get("error", "")):
            broke.append(f"{tool}: {result['error']}")

    assert not broke, "; ".join(broke)

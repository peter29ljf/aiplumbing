"""The graph either hangs together or refuses to load.

Every case here is a mistake that is cheap to make and expensive to find: a node named one
way in `branch` and another in `nodes`, a rules file renamed in one place, a tool that no
longer exists. They all look fine until a conversation walks into them, and then they look
like the model behaving strangely.

The graphs are built as dictionaries rather than by editing YAML text. Patching indented
YAML with string replacement produces files that fail to parse, which then tests the YAML
parser instead of the thing under test.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime.graph import BrokenFlow, load  # noqa: E402
from bat.runtime.project import Project  # noqa: E402

TOOLS = {"a.one", "a.two", "step.finished"}

GOOD: dict[str, Any] = {
    "entry": "start",
    "nodes": {
        "start": {
            "goal": "Say hello.",
            "tools": ["a.one", "step.finished"],
            "sets_status": "New Inquiry",
            "next": "finish",
        },
        "finish": {
            "goal": "Say goodbye.",
            "tools": ["a.two"],
            "sets_status": "Closed",
        },
    },
}


def _load(tmp_path: Path, graph: dict[str, Any], *, rules: tuple[str, ...] = ()):
    """A throwaway project in tmp_path. No monkeypatching: a project is a directory, so a
    test project is a directory too."""
    (tmp_path / "rules").mkdir(exist_ok=True)
    for rule in rules:
        (tmp_path / "rules" / f"{rule}.md").write_text("x")
    (tmp_path / "flow.yaml").write_text(yaml.safe_dump(graph))

    return load(Project(tmp_path), known_tools=TOOLS)


def _but(**changes: Any) -> dict[str, Any]:
    """GOOD with one node replaced, so each test says only what it is about."""
    graph = copy.deepcopy(GOOD)
    for node, spec in changes.items():
        merged = {**graph["nodes"][node], **spec}
        graph["nodes"][node] = {k: v for k, v in merged.items() if v is not None}
    return graph


def test_a_sound_graph_loads(tmp_path: Path):
    flow = _load(tmp_path, GOOD)

    assert flow.entry == "start"
    assert flow["start"].next == "finish"
    assert flow["finish"].is_terminal


def test_a_branch_pointing_at_nothing(tmp_path: Path):
    """The commonest way to break this: rename a node and miss one `branch` entry."""
    graph = _but(start={"next": None, "branch": {"yes": "finish", "no": "finsh"}})

    with pytest.raises(BrokenFlow, match="finsh"):
        _load(tmp_path, graph)


def test_a_missing_rules_file(tmp_path: Path):
    with pytest.raises(BrokenFlow, match="rules/gone.md"):
        _load(tmp_path, _but(start={"rules": ["gone"]}))


def test_a_rules_file_that_is_there(tmp_path: Path):
    flow = _load(tmp_path, _but(start={"rules": ["here"]}), rules=("here",))

    assert flow["start"].rules == ("here",)


def test_a_tool_that_does_not_exist(tmp_path: Path):
    with pytest.raises(BrokenFlow, match="a.three"):
        _load(tmp_path, _but(start={"tools": ["a.one", "step.finished", "a.three"]}))


def test_an_entry_that_is_not_a_node(tmp_path: Path):
    graph = copy.deepcopy(GOOD)
    graph["entry"] = "begin"

    with pytest.raises(BrokenFlow, match="entry"):
        _load(tmp_path, graph)


def test_both_next_and_branch(tmp_path: Path):
    """Which one wins would be whatever the code happens to do, which is not a rule."""
    graph = _but(start={"branch": {"yes": "finish"}})      # `next` is still there

    with pytest.raises(BrokenFlow, match="both"):
        _load(tmp_path, graph)


def test_a_node_nothing_leads_to(tmp_path: Path):
    """Either a typo in somebody's branch or a leftover. It will never run, and it will be
    maintained forever."""
    graph = copy.deepcopy(GOOD)
    graph["nodes"]["stranded"] = {
        "goal": "Nobody comes here.",
        "tools": ["a.two"],
        "sets_status": "Closed",
    }

    with pytest.raises(BrokenFlow, match="stranded"):
        _load(tmp_path, graph)


def test_a_terminal_node_needs_no_way_to_end(tmp_path: Path):
    """Being terminal is what ends it. The engine closes the conversation on the reply,
    because asking the model to announce a fact the graph already holds is one more thing
    it can forget — and it did: the booking, the text, the technician and the follow-up
    all done, the confirmation said, and the conversation left open."""
    flow = _load(tmp_path, _but(finish={"tools": ["a.two"]}))

    assert flow["finish"].is_terminal


def test_a_node_with_no_goal(tmp_path: Path):
    with pytest.raises(BrokenFlow, match="goal"):
        _load(tmp_path, _but(finish={"goal": ""}))


def test_a_node_with_no_status(tmp_path: Path):
    with pytest.raises(BrokenFlow, match="sets_status"):
        _load(tmp_path, _but(finish={"sets_status": ""}))


def test_a_node_that_cannot_say_it_is_done(tmp_path: Path):
    """Without step.finished it has no way to signal an outcome, so the conversation stops
    there — which reads, from outside, as the model refusing to move on."""
    with pytest.raises(BrokenFlow, match="no way to say it is done"):
        _load(tmp_path, _but(start={"tools": ["a.one"]}))


def test_every_problem_is_reported_at_once(tmp_path: Path):
    """Not one per run. Fixing them one at a time is how a five-minute job takes an hour."""
    graph = _but(start={"next": "nowhere"}, finish={"tools": ["a.nine"]})

    with pytest.raises(BrokenFlow) as caught:
        _load(tmp_path, graph)

    assert "nowhere" in str(caught.value)
    assert "a.nine" in str(caught.value)


# ---- and the real one -------------------------------------------------


def test_the_reference_project_is_sound():
    """Without known_tools — the tools are checked once they have been imported."""
    from bat.runtime.project import find

    flow = load(find("plumbing"))

    assert flow.entry in flow.nodes
    assert any(node.is_terminal for node in flow.nodes.values())


def test_no_node_still_carries_an_ending_tool():
    """It was removed everywhere at once; a node keeping it would be offering the model a
    tool that no longer exists."""
    flow = load(__import__("bat.runtime.project", fromlist=["find"]).find("plumbing"))

    for node in flow.nodes.values():
        assert "conversation.end" not in node.tools, node.name


def test_no_node_carries_more_than_a_handful_of_tools():
    """The whole point of the rewrite. A node that quietly grows back to twenty tools has
    given up the thing this was for."""
    flow = load(__import__("bat.runtime.project", fromlist=["find"]).find("plumbing"))

    for node in flow.nodes.values():
        assert len(node.tools) <= 6, f"{node.name} has {len(node.tools)}"


def test_nodes_written_at_the_top_level_are_named_as_such(tmp_path: Path):
    """A generated project wrote every node at the top level with no `nodes:` wrapper.
    "flow.yaml has no `nodes`" reads like the file is empty, and three builds went looking
    for the wrong thing — one of them for an architectural limitation that was not there."""
    (tmp_path / "flow.yaml").write_text(yaml.safe_dump({
        "entry": "greeting",
        "greeting": {"goal": "Say hello.", "tools": ["a.one"], "sets_status": "New"},
        "route": {"goal": "Decide.", "tools": ["a.two"], "sets_status": "New"},
    }))

    with pytest.raises(BrokenFlow, match="look like nodes"):
        load(Project(tmp_path), known_tools=TOOLS)

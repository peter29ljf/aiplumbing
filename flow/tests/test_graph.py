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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from flow.runner.graph import BrokenFlow, load  # noqa: E402

TOOLS = {"a.one", "a.two", "ticket.set_fields", "conversation.end"}

GOOD: dict[str, Any] = {
    "entry": "start",
    "nodes": {
        "start": {
            "goal": "Say hello.",
            "tools": ["a.one", "ticket.set_fields"],
            "sets_status": "New Inquiry",
            "next": "finish",
        },
        "finish": {
            "goal": "Say goodbye.",
            "tools": ["conversation.end"],
            "sets_status": "Closed",
        },
    },
}


def _load(tmp_path: Path, graph: dict[str, Any], *, rules: tuple[str, ...] = ()):
    import flow.runner.graph as module

    (tmp_path / "rules").mkdir(exist_ok=True)
    for rule in rules:
        (tmp_path / "rules" / f"{rule}.md").write_text("x")

    path = tmp_path / "flow.yaml"
    path.write_text(yaml.safe_dump(graph))

    original = module.FLOW_DIR
    module.FLOW_DIR = tmp_path          # rules are looked up beside the yaml
    try:
        return load(path, known_tools=TOOLS)
    finally:
        module.FLOW_DIR = original


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
        _load(tmp_path, _but(start={"tools": ["a.one", "ticket.set_fields", "a.three"]}))


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
        "tools": ["conversation.end"],
        "sets_status": "Closed",
    }

    with pytest.raises(BrokenFlow, match="stranded"):
        _load(tmp_path, graph)


def test_an_ending_that_cannot_end(tmp_path: Path):
    """A terminal node without conversation.end leaves the customer looking at their own
    last message forever."""
    with pytest.raises(BrokenFlow, match="conversation.end"):
        _load(tmp_path, _but(finish={"tools": ["a.two"]}))


def test_a_node_with_no_goal(tmp_path: Path):
    with pytest.raises(BrokenFlow, match="goal"):
        _load(tmp_path, _but(finish={"goal": ""}))


def test_a_node_with_no_status(tmp_path: Path):
    with pytest.raises(BrokenFlow, match="sets_status"):
        _load(tmp_path, _but(finish={"sets_status": ""}))


def test_a_node_that_cannot_say_it_is_done(tmp_path: Path):
    """Without ticket.set_fields it has no way to signal an outcome, so the conversation
    stops there — which reads, from outside, as the model refusing to move on."""
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


def test_the_real_flow_is_sound():
    """Without known_tools — the tools are checked once the simulator exists."""
    flow = load()

    assert flow.entry in flow.nodes
    assert any(node.is_terminal for node in flow.nodes.values())


def test_every_ending_says_something_before_it_ends():
    flow = load()

    for node in flow.nodes.values():
        if node.is_terminal:
            assert "conversation.end" in node.tools, node.name


def test_no_node_carries_more_than_a_handful_of_tools():
    """The whole point of the rewrite. A node that quietly grows back to twenty tools has
    given up the thing this was for."""
    flow = load()

    for node in flow.nodes.values():
        assert len(node.tools) <= 6, f"{node.name} has {len(node.tools)}"

"""The second backend's tools, and the boundary that is the whole reason to be careful.

Claude Code is kept inside one directory by `--add-dir`, enforced by something outside
this code. This loop has nothing like that: the only thing between it and the filesystem
is `_inside`. So that function gets the attention, and the tests for it come first.

No model is called anywhere in here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.builder import agent  # noqa: E402


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "rules").mkdir()
    (tmp_path / "flow.yaml").write_text("entry: greeting\n")
    (tmp_path / "rules" / "greeting.md").write_text("Say hello.\n")
    return tmp_path


def _do(project: Path, name: str, **args) -> str:
    return agent._do(project, name, args, "test", ROOT)


# ---- the boundary -------------------------------------------------------


def test_a_relative_path_inside_is_fine(project: Path):
    assert agent._inside(project, "rules/greeting.md") == project / "rules/greeting.md"


def test_climbing_out_is_refused(project: Path):
    with pytest.raises(agent.OutsideTheProject):
        agent._inside(project, "../../../etc/passwd")


def test_an_absolute_path_elsewhere_is_refused(project: Path):
    with pytest.raises(agent.OutsideTheProject):
        agent._inside(project, "/etc/passwd")


def test_a_symlink_pointing_out_is_refused(project: Path, tmp_path: Path):
    """A string check for `..` catches neither this nor the one above. Resolving first
    catches both, which is why it resolves first."""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secrets")
    (project / "sneaky.md").symlink_to(outside)

    with pytest.raises(agent.OutsideTheProject):
        agent._inside(project, "sneaky.md")


def test_a_path_that_only_looks_like_it_climbs_out_is_fine(project: Path):
    """`rules/../flow.yaml` lands back inside. Refusing it would be a boundary that is
    also wrong, which teaches the loop to work around the boundary."""
    assert agent._inside(project, "rules/../flow.yaml") == project / "flow.yaml"


def test_the_refusal_says_what_to_do_instead(project: Path):
    """A refusal a model cannot act on gets retried in a slightly different wrong way
    until the step budget runs out."""
    try:
        agent._inside(project, "/etc/passwd")
    except agent.OutsideTheProject as refused:
        assert "relative path" in str(refused)


def test_a_tool_given_an_outside_path_answers_rather_than_crashing(project: Path):
    """The loop has to keep going. A traceback ends the turn; a refusal it can read is a
    correction it can act on."""
    from bat.builder.agent import OutsideTheProject

    with pytest.raises(OutsideTheProject):
        _do(project, "read_file", path="../../../etc/passwd")


# ---- the tools ----------------------------------------------------------


def test_reading_something_that_is_not_there_is_not_an_error(project: Path):
    """Half the reads in a build are "is this written yet". An exception for the normal
    answer to that question is noise."""
    assert "no rules/nothing.md yet" in _do(project, "read_file", path="rules/nothing.md")


def test_writing_makes_the_directories(project: Path):
    _do(project, "write_file", path="scenarios/01_first.yaml", text="id: first\n")

    assert (project / "scenarios/01_first.yaml").read_text() == "id: first\n"


def test_an_edit_that_matches_nothing_says_so(project: Path):
    """`str.replace` silently doing nothing has cost this project a morning more than
    once — the pass count did not move and it read as confirmation."""
    said = _do(project, "edit_file", path="flow.yaml", old="not there", new="x")

    assert "not in the file" in said
    assert (project / "flow.yaml").read_text() == "entry: greeting\n"    # untouched


def test_an_edit_that_matches_twice_is_refused(project: Path):
    (project / "flow.yaml").write_text("a: 1\nb: 2\na: 1\n")

    said = _do(project, "edit_file", path="flow.yaml", old="a: 1", new="a: 9")

    assert "appears 2 times" in said
    assert (project / "flow.yaml").read_text().count("a: 1") == 2        # untouched


def test_an_edit_that_matches_once_happens(project: Path):
    _do(project, "edit_file", path="rules/greeting.md", old="hello", new="good morning")

    assert "good morning" in (project / "rules/greeting.md").read_text()


def test_listing_skips_the_noise(project: Path):
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "x.pyc").write_text("")

    listed = _do(project, "list_files")

    assert "flow.yaml" in listed
    assert "pycache" not in listed


def test_search_finds_the_line_and_says_where(project: Path):
    found = _do(project, "search", text="hello")

    assert "rules/greeting.md:1" in found


def test_search_with_no_hits_says_so_plainly(project: Path):
    assert _do(project, "search", text="zzzz") == "No matches."


def test_an_unknown_tool_is_answered_not_raised(project: Path):
    assert "No tool called" in _do(project, "invent_something")


# ---- reads may reach the kit, writes may not ----------------------------


def test_the_kit_can_be_read_when_it_is_offered(project: Path, tmp_path: Path):
    """A build copies its shapes from the preset tools and rule patterns. The first real
    run tried to list them, was refused, and spent a dozen steps grepping for what it
    could have read in one."""
    kit = tmp_path.parent / "kit"
    kit.mkdir(exist_ok=True)
    (kit / "service.py").write_text("# the tools\n")

    assert agent._inside(project, str(kit / "service.py"), readable=(kit,))


def test_the_kit_is_still_refused_when_it_is_not_offered(project: Path, tmp_path: Path):
    kit = tmp_path.parent / "kit"
    kit.mkdir(exist_ok=True)

    with pytest.raises(agent.OutsideTheProject):
        agent._inside(project, str(kit))


def test_writing_never_takes_the_readable_list(project: Path, tmp_path: Path):
    """The asymmetry is the whole point. `write_file` calls `_inside` without `readable`,
    so nothing outside the project is writable however the loop asks."""
    kit = tmp_path.parent / "kit"
    kit.mkdir(exist_ok=True)

    with pytest.raises(agent.OutsideTheProject):
        agent._do(project, "write_file", {"path": str(kit / "x.md"), "text": "no"},
                  "test", ROOT, (kit,))


def test_listing_the_kit_does_not_crash_on_paths_outside_the_project(project: Path,
                                                                    tmp_path: Path):
    kit = tmp_path.parent / "kit"
    kit.mkdir(exist_ok=True)
    (kit / "service.py").write_text("# the tools\n")

    listed = agent._do(project, "list_files", {"path": str(kit)}, "test", ROOT, (kit,))

    assert "service.py" in listed


# ---- a tool module that registers nothing --------------------------------


def test_a_tools_file_with_no_decorator_says_what_is_wrong(tmp_path: Path):
    """It presents as "wants the tool 'x', which does not exist" — which reads like a typo
    in flow.yaml and sends you to the wrong file. A generated project wrote six modules in
    tools/ trying to work out why, and the answer was a missing decorator."""
    from bat.runtime import registry
    from bat.runtime.project import Project

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "mine.py").write_text(
        "def do_something(ticket):\n    return {'ok': True}\n")

    with pytest.raises(registry.NoToolsRegistered, match="@tool decorator"):
        registry.load_tools(Project(tmp_path))


def test_a_helper_module_is_left_alone(tmp_path: Path):
    """Underscore-prefixed files are skipped, so a project can keep shared code in tools/
    without it being mistaken for a tool that failed to register."""
    from bat.runtime import registry
    from bat.runtime.project import Project

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "_shared.py").write_text("WORDS = 'hello'\n")

    assert registry.load_tools(Project(tmp_path))       # the kit's, and no complaint

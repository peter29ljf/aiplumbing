"""Asking why it stalled, when it did not stall.

Two mistakes lived here, and the second only became visible once the first was fixed.

**It claimed a budget it never checked.** Every conversation that failed while the flow
was still moving was told it had "run out of turns", whatever the numbers said. One had
used two of its thirty. A verdict that infers its own cause is the single failure this
whole classifier exists to prevent, and it was doing it in its most common branch.

**It fired on conversations that finished cleanly.** `world.ended` is not the same thing
as the conversation having ended: a flow can walk to its last step, sign off, and leave
that flag false. So five scenarios in one run — every one of which had gone perfectly and
failed on a phrase it did or did not say — were each handed an explanation of a stall that
never happened.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import diagnose  # noqa: E402
from bat.runtime.graph import Flow, Node  # noqa: E402
from bat.runtime.project import Project  # noqa: E402


@dataclass
class FakeStep:
    node: str
    text: str = ""
    said: bool = True
    seconds: float = 1.0
    tools: list[str] = field(default_factory=list)
    offered: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    delta: dict[str, Any] = field(default_factory=dict)
    saw: str = ""


def _flow() -> Flow:
    project = Project(Path("/nowhere"))
    nodes = {
        name: Node(project=project, name=name, goal="x", rules=(),
                   tools=("calendar.find_slots", "step.finished"), sets_status="s",
                   next=nxt, branch={})
        for name, nxt in (("offer", "book"), ("book", None))
    }
    return Flow(project=project, entry="offer", nodes=nodes)


# The branch under test is the "still moving" one, which needs the last few steps to span
# more than one node — a conversation that walked somewhere and then stopped.
MOVING = [FakeStep(node="offer", offered=["step_finished"]),
          FakeStep(node="book", offered=["step_finished"])]


def _result(**kw):
    from types import SimpleNamespace
    base = dict(id="x", passed=False, problems=[], nodes=["offer"], turns=2, budget=30,
                stopped="", steps=list(MOVING),
                snapshot={"ended": False, "tickets": {}}, transcript=[], smells=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_it_does_not_say_the_budget_ran_out_when_it_did_not():
    said = diagnose._why_it_stalled(_result(turns=2, budget=30), _flow(), list(MOVING))

    assert said
    assert "ran out" not in said[0].because
    assert "2 turn(s) of 30" in said[0].because


def test_it_does_say_so_when_the_numbers_agree():
    said = diagnose._why_it_stalled(_result(turns=30, budget=30), _flow(), list(MOVING))

    assert "ran out of turns after 30" in said[0].because


def test_it_repeats_what_the_runner_recorded_rather_than_guessing():
    said = diagnose._why_it_stalled(_result(turns=4, budget=30, stopped="stalled"),
                                    _flow(), list(MOVING))

    assert "stalled" in said[0].because


def test_it_says_plainly_when_nothing_recorded_why():
    said = diagnose._why_it_stalled(_result(turns=4, budget=30, stopped=""), _flow(), list(MOVING))

    assert "nothing recorded why" in said[0].because


def test_a_conversation_that_finished_is_never_asked_why_it_stalled():
    """The one that only became visible once the verdict stopped lying about turns."""
    verdicts = diagnose.diagnose(_result(stopped="the flow ended"), _flow())

    assert not any("still moving" in v.because for v in verdicts)


def test_a_customer_who_left_is_not_a_stall_either():
    verdicts = diagnose.diagnose(_result(stopped="the customer left"), _flow())

    assert not any("still moving" in v.because for v in verdicts)


def test_a_conversation_that_really_stopped_dead_still_gets_an_explanation():
    verdicts = diagnose.diagnose(_result(stopped="stalled"), _flow())

    assert any("still moving" in v.because for v in verdicts)

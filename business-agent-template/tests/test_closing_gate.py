"""What a last step must have done before it can sign off.

The gate exists for one failure: a booking step that says "you're all set" having created
no appointment, sent no text and told nobody. It reads what the tools did, never what the
model said, and that is right.

What was wrong was its idea of "done": every tool the node held, minus two named
exceptions. That makes a tool compulsory the moment it is granted. A step answering
general questions was given the delivery checker so it could answer a question about
delivery — and a customer who asked about opening hours, having no address to check, left
it unable to finish. It went round three times and the scenario failed for circling.

A lookup is never anybody's job. An action always is. `once=True` already draws that line.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import registry  # noqa: E402


def setup_module(_module):
    registry.load_tools(None)


def test_an_action_is_the_work():
    assert registry.reaches_outside("calendar.create_appointment")
    assert registry.reaches_outside("sms.send")
    assert registry.reaches_outside("technician.notify")
    assert registry.reaches_outside("escalate.raise")


def test_a_lookup_is_not():
    """Nobody's job is to have looked something up."""
    assert not registry.reaches_outside("clock.now")
    assert not registry.reaches_outside("calendar.find_slots")
    assert not registry.reaches_outside("crm.lookup_by_phone")
    assert not registry.reaches_outside("rules.get_service_options")


def test_bookkeeping_is_not_work_either():
    assert not registry.reaches_outside("ticket.set_fields")
    assert not registry.reaches_outside("step.finished")


def test_a_tool_nobody_registered_is_not_work():
    """A missing tool must not become a thing the step is held to."""
    assert not registry.reaches_outside("nothing.like_this")


def test_granting_a_lookup_to_a_last_step_does_not_make_it_compulsory():
    """The regression. Four lookups and one action: only the action is owed."""
    node_tools = ("clock.now", "rules.get_service_options", "calendar.find_slots",
                  "ticket.set_fields", "sms.send")

    owed = [t for t in node_tools if registry.reaches_outside(t)]

    assert owed == ["sms.send"]


# ---- and all three engines have to agree ---------------------------------


def test_every_engine_asks_the_same_question_of_a_last_step():
    """The rule lived in three copies and one of them was fixed.

    `engine.py`, `per_node.py` and `per_skill.py` each carried their own idea of what a
    terminal node owes. Changing the in-process one and not the other two meant that on
    the Claude Code engines a step answering general questions still had to call the
    delivery checker before it could finish — for a customer asking about opening hours,
    who had no address to check. It never ended, so the scenario failed every single time
    on the strong model while passing on the weak one, and the score read as the strong
    model being worse.

    Three copies of a rule is three chances to fix one of them.
    """
    import inspect
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    sources = {
        "engine": (root / "bat" / "runtime" / "engine.py").read_text(),
        "per_node": (root / "bat" / "engines" / "per_node.py").read_text(),
        "per_skill": (root / "bat" / "engines" / "per_skill.py").read_text(),
    }

    stale = [name for name, text in sources.items()
             if "reaches_outside" not in text.split("def _undone")[1][:600]]

    assert not stale, (f"{', '.join(stale)} still decides for itself what a last step "
                       f"owes, instead of asking registry.reaches_outside")


def test_the_nudge_is_one_function_that_all_three_engines_reach():
    """`_still_here` was the other rule living in three copies, and the shortest of them
    had dropped the reason — "a later step has it; finishing is how they get it" — which
    is the half that makes a rule followed. A weaker nudge on one engine is invisible: the
    step just talks a little longer.

    There is one function now. This checks that all three ask it, and that it still says
    the thing worth saying."""
    from types import SimpleNamespace

    from bat.engines.per_node import Conversation as PerNode
    from bat.engines.per_skill import SkillConversation as PerSkill
    from bat.runtime.engine import Conversation as InProcess
    from bat.runtime.engine import nudge

    node = SimpleNamespace(name="offer", is_terminal=False)
    said = nudge(node, 4)
    for phrase in ("step.finished", "a later step has it", "repeating yourself"):
        assert phrase in said, f"the nudge no longer says {phrase!r}"

    in_process = InProcess.__new__(InProcess)
    in_process.messages = [{"role": "assistant", "content": "x"}] * 4
    per_node = PerNode.__new__(PerNode); per_node.replies_here = 4
    per_skill = PerSkill.__new__(PerSkill); per_skill.replies_here = 4

    assert {in_process._still_here(node), per_node._still_here(node),
            per_skill._still_here(node)} == {said}


def test_no_engine_nudges_a_last_step():
    """A terminal step has nothing to finish into, and telling it to call `step.finished`
    is how one spent three turns retrying a tool it does not have."""
    from types import SimpleNamespace

    from bat.engines.per_node import Conversation as PerNode
    from bat.engines.per_skill import SkillConversation as PerSkill
    from bat.runtime.engine import Conversation as InProcess

    node = SimpleNamespace(name="book", is_terminal=True)
    in_process = InProcess.__new__(InProcess)
    in_process.messages = [{"role": "assistant", "content": "x"}] * 4
    per_node = PerNode.__new__(PerNode); per_node.replies_here = 4
    per_skill = PerSkill.__new__(PerSkill); per_skill.replies_here = 4

    assert not in_process._still_here(node)
    assert not per_node._still_here(node)
    assert not per_skill._still_here(node)

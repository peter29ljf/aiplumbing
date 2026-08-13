"""A scenario that ends and leaves its process running.

Every conversation on the Claude Code engine is a live `claude` process holding about
370 MB. Nothing closed them: fifty-four scenarios meant fifty-four processes, twenty
gigabytes wanted on a sixteen-gigabyte machine, five in swap, load average forty-four.
After fifty minutes the suite had produced no results at all.

**It did not look like a leak.** It looked like the model being slow, and the first
instinct was to blame concurrency. The machine was not computing; it was paging. Nothing
in the suite failed — an assertion cannot see a process — so there was no failure to read,
only a number that would not arrive.

The fix has two halves and both matter. Closing at the end of the suite bounds nothing:
the tenth scenario starts long before the first is collected. Closed when its own scenario
ends, the number of processes alive is the number of scenarios running.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import harness  # noqa: E402


class Talk:
    """Enough of a conversation for `run_one` to drive, and it remembers being closed."""

    def __init__(self, world, llm, flow, **_):
        self.world, self.flow = world, flow
        self.finished = True                  # ends on the first turn
        self.closed = False
        opened.append(self)

    @property
    def tags(self):
        return {}

    def say(self, _text):
        from bat.runtime.engine import Turn
        return Turn(reply="Booked. Nothing further.")

    def close(self):
        self.closed = True


opened: list[Talk] = []


def test_a_finished_scenario_closes_its_own_session(tmp_path, monkeypatch):
    from bat.runtime import project as projects

    opened.clear()
    monkeypatch.setattr(harness, "Conversation", Talk)
    project = projects.find("travel")
    scenario = sorted(project.scenarios_dir.glob("*.yaml"))[0]

    harness.run_one(scenario, lambda: SimpleNamespace(
        chat=lambda *a, **k: SimpleNamespace(content="DONE"),
        usage=SimpleNamespace(as_dict=lambda: {})), project)

    assert opened, "the test did not drive a conversation at all"
    assert all(t.closed for t in opened), (
        "a scenario ended and left its process running — fifty-four of these put a "
        "sixteen-gigabyte machine into swap and produced no results for fifty minutes")


def test_a_conversation_with_nothing_to_close_is_left_alone():
    """The in-process engine holds no process and has no `close`. Reaching for one must
    not turn a passing scenario into an error."""
    from bat.runtime import project as projects
    from bat.runtime.engine import Turn

    class Bare:
        def __init__(self, world, llm, flow, **_):
            self.finished, self.flow = True, flow

        @property
        def tags(self):
            return {}

        def say(self, _text):
            return Turn(reply="Booked.")

    import unittest.mock as mock

    project = projects.find("travel")
    scenario = sorted(project.scenarios_dir.glob("*.yaml"))[0]
    with mock.patch.object(harness, "Conversation", Bare):
        result = harness.run_one(scenario, lambda: SimpleNamespace(
            chat=lambda *a, **k: SimpleNamespace(content="DONE"),
            usage=SimpleNamespace(as_dict=lambda: {})), project)

    assert result is not None

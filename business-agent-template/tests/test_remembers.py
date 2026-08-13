"""A tool that says it remembers something, and does not.

`remembers=("phone",)` tells the engine to copy that fact onto the ticket without the
model being asked to write it down. The engine looks for the name among the tool's
arguments and in the dict it returns. If it is in neither, nothing happens — silently.
The declaration reads like a guarantee and buys nothing, and the fact reaches the next
step only when the model happens to write it by hand.

Twice in one day:

- `consultant.send_enquiry` declared `remembers=("sent_to",)` and returned `{"to": ...}`.
  Three travel scenarios asserted on a ticket field nothing ever set.
- `menu.check_items` declared `remembers=("dishes",)` and returned `{"results": [...]}`.
  The order never reached the ticket, so the quote came back zero, so a free-delivery
  threshold was never crossed — and the scenario read as flaky for a week.

Both were invisible until a suite run, and both are a name mismatch a second can find.
"""

from __future__ import annotations

import inspect
import re
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
def test_everything_a_tool_remembers_is_something_it_could_produce(name):
    project = projects.find(name)
    registry.load_tools(project)

    empty = []
    for tool, spec in registry._TOOLS.items():
        wanted = spec["remembers"]
        if not wanted:
            continue
        handler = spec["handler"]
        takes = set(inspect.signature(handler).parameters)
        try:
            body = inspect.getsource(handler)
        except OSError:                       # built in some other way; nothing to read
            continue
        # A returned key, written as a literal. Not perfect — a dict built in a loop can
        # hide one — but every real miss so far has been a plain `"name": value`.
        returns = set(re.findall(r'["\'](\w+)["\']\s*:', body))
        for key in wanted:
            if key not in takes and key not in returns:
                empty.append(f"{tool} remembers {key!r}, which it neither takes nor returns")

    assert not empty, "; ".join(empty)

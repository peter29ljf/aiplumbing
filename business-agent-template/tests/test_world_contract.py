"""`world.md` is the only thing a tool author reads. It has to be the whole world.

The builder cannot read the engine — it is handed `world.md` and writes tools against it.
A takeaway project once wrote `world.place_order(...)` and `world.book_table(...)`, neither
of which exists, and both tools were dead on arrival. That is the failure this document was
written to stop.

Then the document drifted the other way: it listed eighteen members while the code reached
thirty-six. Everything omitted was engine bookkeeping — `done`, `repeats`, `ended` — which
looked safe to leave out right up until somebody wrote a second world from the document.
The first `once=True` tool reached `world.done` and crashed inside `registry.call`.

So the rule is both directions, and both are checked here:

- **nothing in the code missing from the document** — or a tool author cannot know it exists
- **nothing in the document missing from the code** — or they write against something gone

No model, no network. This is the same kind of test as `test_tool_wiring.py`: it costs
nothing and it catches a class of failure no scenario run can reach.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import project as projects  # noqa: E402
from bat.runtime.sim import World  # noqa: E402

DOC = ROOT / "bat" / "presets" / "world.md"

# Where the listing lives. Everything indented four spaces between this heading and the
# next one is read as "this member is documented".
SECTION = "## The whole surface"

# `_`-prefixed members are the world's own business. `Refused` and the dataclasses arrive
# through the module, not the world.
_ENTRY = re.compile(r"^ {4}(\w+)")


def _documented() -> set[str]:
    lines = DOC.read_text().splitlines()
    start = lines.index(SECTION) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    return {m.group(1) for m in map(_ENTRY.match, lines[start:end]) if m}


def _real() -> set[str]:
    world = World(now="2026-08-05T10:00:00-07:00",
                  rules=projects.find("plumbing").business_rules())
    return {n for n in set(dir(world)) | set(vars(world)) if not n.startswith("_")}


def test_nothing_the_world_has_is_left_undocumented():
    """A tool author who cannot see it cannot use it, and guesses instead."""
    missing = sorted(_real() - _documented())

    assert not missing, (
        f"{DOC.name} does not mention: {missing}. A tool written from this document "
        f"cannot reach them, and a second world written from it will not carry them."
    )


def test_nothing_documented_has_gone_away():
    """The other direction. A member renamed in `sim.py` and left in the document sends
    every future tool at a name that raises `AttributeError` mid-conversation."""
    invented = sorted(_documented() - _real())

    assert not invented, (
        f"{DOC.name} promises members the world does not have: {invented}."
    )


def test_the_section_is_actually_being_read():
    """A regex that quietly matched nothing would make both tests above pass forever."""
    assert len(_documented()) > 20

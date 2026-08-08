"""A step is not allowed to say it is finished having achieved nothing.

Three gates now, and this was the missing one:

- `nudge` — a step that keeps talking and never finishes
- `_undone` — a last step signing off before it has done the outward thing
- `not_yet` — a step that finishes without what it was told to write down

`new_customer`'s goal says, in as many words, "take their name, service address and email".
A customer answered the question by repeating their phone number; the step opened a record
with both fields blank, called `step.finished`, and the conversation ran all the way to a
booked visit with nowhere to send anybody. Every prompt was right. Nothing checked.

The check reads the **ticket**, never the transcript, because the ticket is what the next
step is handed — a customer can say their address and the step can still not have written
it down.

No model, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import project as projects  # noqa: E402
from bat.runtime import registry  # noqa: E402
from bat.runtime.engine import not_yet  # noqa: E402
from bat.runtime.graph import BrokenFlow, load  # noqa: E402
from bat.runtime.project import Project  # noqa: E402


def _node(*needs: str):
    return SimpleNamespace(name="new_customer", needs=tuple(needs))


# ---- the gate itself ----------------------------------------------------


def test_a_blank_field_is_not_a_field():
    """The exact shape of the bug: the tool was called, the record exists, the fields are
    empty strings. `"" or None` reads as absent, and it is."""
    said = not_yet(_node("name", "address"), {"phone": "604-555-0166", "name": "",
                                              "address": "   "})

    assert "name" in said and "address" in said


def test_a_missing_key_is_the_same_as_a_blank_one():
    assert not_yet(_node("name"), {"phone": "604-555-0166"})


def test_a_node_that_has_what_it_needs_goes_through():
    assert not_yet(_node("name", "address"),
                   {"name": "Dana", "address": "12 Elm St, Burnaby"}) == ""


def test_a_node_that_needs_nothing_is_never_held():
    """Most nodes. A gate that fired on every step would be `_undone`'s mistake again —
    that one made a tool compulsory the moment it was granted."""
    assert not_yet(_node(), {}) == ""


def test_the_refusal_says_what_to_do_with_it():
    """A refusal that only says no gets retried unchanged three times and then the step is
    failed for circling. This has happened here twice."""
    said = not_yet(_node("address"), {})

    assert "ticket.set_fields" in said, "no way to fix it was offered"
    assert "ask them" in said.lower(), "not told what to do if the customer never said it"


# ---- one function, three engines ---------------------------------------


def test_every_engine_asks_the_same_question():
    """The two rules that previously lived in three copies both drifted — the nudge went
    silently weaker on one engine, the closing gate was plainly broken on two. This one
    starts as a single function and this is what keeps it that way."""
    from bat.engines.per_node import Conversation as PerNode
    from bat.engines.per_skill import SkillConversation as PerSkill
    from bat.runtime.engine import Conversation as InProcess

    node = _node("name", "address")
    tags = {"phone": "604-555-0166"}
    expected = not_yet(node, tags)
    assert expected

    ticket = SimpleNamespace(tags=tags)
    world = SimpleNamespace(ticket=lambda _id: ticket)

    said = set()
    for engine in (InProcess, PerNode, PerSkill):
        talk = engine.__new__(engine)
        talk.world, talk.ticket_id = world, "TK-1"
        said.add(talk._not_yet(node))

    assert said == {expected}


# ---- what the graph refuses to load ------------------------------------


def test_a_node_that_needs_something_and_cannot_write_it_is_caught_at_load(tmp_path: Path):
    """Otherwise it is discovered as a step going round in circles halfway through a
    conversation, which reads as the model refusing to move on."""
    import yaml

    registry.load_tools(None)
    (tmp_path / "rules").mkdir(exist_ok=True)
    (tmp_path / "flow.yaml").write_text(yaml.safe_dump({
        "entry": "a",
        "nodes": {
            # `step.finished` records nothing and `clock.now` remembers nothing, so there
            # is no way for `name` to ever reach the ticket from here.
            "a": {"goal": "g", "sets_status": "s", "needs": ["name"],
                  "tools": ["clock.now", "step.finished"], "next": "b"},
            "b": {"goal": "g", "sets_status": "s", "tools": []},
        },
    }))

    try:
        load(Project(tmp_path), known_tools=registry.names())
    except BrokenFlow as broken:
        assert "needs" in str(broken) and "ticket.set_fields" in str(broken)
    else:
        raise AssertionError("a node that can never finish was accepted")


# ---- the project it was written for ------------------------------------


def test_plumbing_guards_the_facts_a_later_step_cannot_recover():
    """`property_route` branches on `property_type` and holds no tool that writes to the
    ticket. If `property_ask` did not record it, nothing downstream can."""
    registry.load_tools(None)
    flow = load(projects.find("plumbing"), known_tools=registry.names())

    assert "address" in flow["new_customer"].needs
    assert "property_type" in flow["property_ask"].needs
    assert "ticket.set_fields" not in flow["property_route"].tools, (
        "property_route can write now, so the reason for the guard above has changed"
    )

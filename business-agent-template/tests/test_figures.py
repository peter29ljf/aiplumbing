"""A figure the customer is given has to have come from somewhere.

The state-delta detector answers "did the thing happen". It cannot answer "is the thing
you described the thing that happened": a step that says *"you're booked for Tuesday at
11, reference TB-0007"* passes it the moment any appointment exists, whatever the
reference or the day. Every project's `always.md` already forbids quoting an unlooked-up
figure, and until now nothing checked it.

**This is the fourth detector in this project that reads what was said, and the previous
three were all confidently wrong at first.** So the scope is deliberately narrow: money
with a currency symbol, and reference-shaped identifiers. Not times and not dates — a
customer says "the 11:00 one" constantly, and a detector that cannot tell their words from
an invention is a detector that blames correct behaviour.

A figure is legitimate if it appeared in a tool's answer, on the ticket, or in something
the customer said. Every case below is that rule.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import diagnose  # noqa: E402


@dataclass
class FakeStep:
    node: str
    text: str = ""
    saw: str = ""                       # what the tools answered, this step
    delta: dict[str, Any] = field(default_factory=dict)
    said: bool = True
    seconds: float = 1.0
    tools: list[str] = field(default_factory=list)
    offered: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)


@dataclass
class FakeResult:
    steps: list[FakeStep]
    transcript: list[tuple[str, str]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=lambda: {"tickets": {}})


def _blamed(text: str, *, saw: str = "", customer: str = "",
            tags: dict | None = None) -> list[str]:
    result = FakeResult(
        steps=[FakeStep(node="book", text=text, saw=saw)],
        transcript=[("customer", customer)] if customer else [],
        snapshot={"tickets": {"TK-1": {"status": "s", "tags": tags or {}, "history": []}}},
    )
    return [v.because for v in diagnose._figures_from_nowhere(result)]


# ---- what it must catch -------------------------------------------------


def test_a_price_nothing_looked_up():
    """The rule every `always.md` already states and nothing enforced."""
    assert _blamed("The visit is $180 including the first half hour.")


def test_an_invented_reference():
    assert _blamed("You're all set — your booking reference is TB-0007.")


def test_a_figure_that_is_close_but_not_the_one_the_tool_gave():
    """The failure the delta check cannot see: the booking happened, the number is wrong."""
    assert _blamed("That'll be $1,850.", saw='{"fee": "$1,800"}')


# ---- what it must not ---------------------------------------------------


def test_a_price_the_tool_answered():
    assert not _blamed("The visit is $180.", saw='{"callout": "$180", "ok": true}')


def test_the_same_figure_written_without_the_comma():
    """`$1,800` from a step against `1800` from a tool is the same money."""
    assert not _blamed("It starts at $1,800.", saw='{"corporate_year_end": 1800}')


def test_a_figure_the_customer_gave():
    """"Around $6,000 total" comes back in the summary, and it is theirs."""
    assert not _blamed("Noted — around $6,000 in total.",
                       customer="our budget is about $6,000 total")


def test_a_figure_already_on_the_ticket():
    """An earlier step looked it up and wrote it down; this step reads the ticket."""
    assert not _blamed("Your quote stands at $420.", tags={"quote": "$420"})


def test_a_reference_the_tool_returned():
    assert not _blamed("Your reference is TB-0007.", saw='{"ref": "TB-0007"}')


def test_a_figure_the_tool_gave_on_an_earlier_call_of_the_same_step():
    """A step quotes on one model call and speaks on the next, and the two are separate
    `Step` records. Judged per call the figure came from nowhere; judged over the node the
    tool had just answered it. The same per-call-versus-per-node mistake the delta
    detector made, and it produced the same false accusation — "$4.00" from a step that
    had called the quoting tool one breath earlier."""
    result = FakeResult(steps=[
        FakeStep(node="confirm", text="", saw='{"delivery_fee": 4, "total": 34.5}'),
        FakeStep(node="confirm", text="Your order comes to $34.50."),
    ])

    assert [v.because for v in diagnose._figures_from_nowhere(result)] == []


def test_a_time_is_never_checked():
    """Deliberately out of scope. Customers say times constantly, and a detector that
    cannot tell "the 11:00 one" from an invention blames correct behaviour — which is
    exactly how the previous three detectors in this project went wrong."""
    assert not _blamed("Tuesday at 11:00 it is.")


def test_a_year_is_not_money():
    assert not _blamed("Looking at spring 2026, then.")


def test_a_phone_number_is_not_a_reference():
    assert not _blamed("I have you on 604-555-0166.", customer="604-555-0166")


def test_a_bare_number_with_no_currency_is_left_alone():
    """"Two adults and 3 children" is not a price. Only a currency symbol makes it one."""
    assert not _blamed("So that's 2 adults and 3 children, 14 nights.")

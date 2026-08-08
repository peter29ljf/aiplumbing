"""An assertion nobody checks is worse than no assertion.

It reads as coverage and provides none, and it is the fourth time in this project that
the instrument turned out to be quietly not measuring. This one was the worst of them:
thirty-seven dead assertions across three projects, every travel scenario asserting
`enquiries: 1` against a tool that recorded nothing in the world, and a suite reporting
13/15 while the central action of the business — sending the enquiry to a consultant —
had never once succeeded in any run.

Two things hold it shut now, and both are tested here: a key the judge does not
understand fails the scenario instead of being skipped, and a project's own nouns become
checkable by being recorded in the world.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import harness  # noqa: E402
from bat.runtime.sim import World  # noqa: E402


def _result(snapshot: dict, transcript=()):
    return SimpleNamespace(
        id="x", problems=[], passed=True, snapshot=snapshot, nodes=["handover"],
        transcript=list(transcript),
        wrong=lambda p: (_result_problems.append(p), None)[1],
    )


_result_problems: list[str] = []


def _judge(expect: dict, snapshot: dict, *, finished=True, transcript=()) -> list[str]:
    _result_problems.clear()
    snapshot = {"tickets": {}, "appointments": [], "texts": [], "emails": [],
                "technician_messages": [], "escalations": [], "followups": [],
                **snapshot}
    harness._judge(_result(snapshot, transcript), expect,
                   SimpleNamespace(finished=finished), None)
    return list(_result_problems)


# ---- the hole ------------------------------------------------------------


def test_an_assertion_nothing_understands_fails_the_scenario():
    """Verbatim from travel: every one of its fifteen scenarios asked for this."""
    said = _judge({"enquiries": 1}, {})

    assert said, "an unknown key was skipped in silence"
    assert "enquiries" in said[0]


def test_the_complaint_says_what_could_have_been_counted():
    """Because the two causes need different fixes — a tool that records nothing, or a
    typo — and the message is the only place anybody finds out which."""
    said = _judge({"orders": 1}, {})

    assert "appointments" in said[0] and "texts" in said[0]


def test_a_project_noun_recorded_in_the_world_is_counted_like_any_other():
    said = _judge({"enquiries": 1}, {"enquiries": [{"to": "Priya"}]})

    assert said == []


def test_none_of_something_is_a_real_zero_when_the_tools_can_record_it():
    """The false positive the first version of this guard produced, on the run that
    proved it worked. `enquiries: 0` is what every decline scenario asserts — and an
    empty world has no `enquiries` key at all, so the guard called four passing scenarios
    unchecked. The world is seeded from `Project.records()` now, which reads the nouns
    off the tool source: a noun some tool records exists at zero, and a noun nothing has
    ever recorded is still an unchecked assertion."""
    assert _judge({"enquiries": 0}, {"enquiries": []}) == []
    assert _judge({"enquiries": 0}, {}), "a noun no tool records must still be reported"


def test_and_counted_wrong_when_it_did_not_happen():
    said = _judge({"enquiries": 1}, {"enquiries": []})

    assert said and "expected 1" in said[0]


def test_world_record_reaches_the_snapshot():
    """The half of the mechanism that lives in the world rather than the judge."""
    world = World(now="2026-08-06T10:00:00-07:00",
                  rules={"company": {"timezone": "America/Vancouver"}})
    world.record("enquiries", {"to": "Sam"})

    assert world.snapshot()["enquiries"] == [{"to": "Sam"}]


# ---- what must keep working ---------------------------------------------


def test_the_keys_it_has_always_known_are_not_reported_as_unchecked():
    said = _judge({"reaches": "handover", "ticket_status": "With Consultant",
                   "finishes": True},
                  {"tickets": {"TK-1": {"status": "With Consultant", "tags": {},
                                        "history": []}}})

    assert said == []


def test_a_ticket_tag_is_checkable():
    """`sent_to: Sam` was one of the dead ones. It is a fact left on the ticket, not a
    list to count, so it needed its own shape rather than another silent skip."""
    tickets = {"TK-1": {"status": "s", "tags": {"sent_to": "Sam"}, "history": []}}

    assert _judge({"ticket_tags": {"sent_to": "Sam"}}, {"tickets": tickets}) == []
    said = _judge({"ticket_tags": {"sent_to": "Priya"}}, {"tickets": tickets})
    assert said and "expected 'Priya'" in said[0]


def test_a_follow_up_chase_is_still_told_apart_from_being_told_about_the_job():
    """Counting them together would make the number meaningless the moment a scenario
    runs the follow-up clock, which is why this one is not just `len()`."""
    messages = [{"kind": "handover"}, {"kind": "followup"}]

    assert _judge({"technician_messages": 1}, {"technician_messages": messages}) == []

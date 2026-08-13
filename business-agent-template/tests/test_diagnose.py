"""Did a step really claim something, or did it only say a word?

This is the one detector that reads what was said rather than what was done, and reading
words is how you get a detector that is confidently wrong. It was: a step said

    "I won't say you're booked just yet, because that confirmation happens in the next
     step, but I'm passing it through"

— careful, correct, and reported as a lie because the sentence contained "booked".

The rule the docs give the generator is "ban the sentence somebody would regret, not the
word it contains". This file holds that rule to the code that enforces it.

Every case is a real line from a real run. No model is called anywhere.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
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
    problems: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    passed: bool = False
    snapshot: dict[str, Any] = field(default_factory=lambda: {"ended": True})


def _flow(**tools_by_node: tuple[str, ...]) -> Flow:
    project = Project(Path("/nowhere"))
    nodes = {
        name: Node(project=project, name=name, goal="x", rules=(), tools=tools,
                   sets_status="s", next=None, branch={})
        for name, tools in tools_by_node.items()
    }
    return Flow(project=project, entry=next(iter(nodes)), nodes=nodes)


def _blamed(node: str, text: str, flow: Flow, **delta: Any) -> list[str]:
    result = FakeResult(steps=[FakeStep(node=node, text=text, delta=delta)])
    return [v.because for v in diagnose._spoke_out_of_turn(result, flow)]


# ---- what it must catch -------------------------------------------------


def test_a_booking_announced_that_did_not_happen():
    """The one this exists for. `offer_options` said "your visit is set for today at
    11:00" — a booking announced by a step that only lists what is free."""
    flow = _flow(offer_options=("calendar.find_slots",))

    assert _blamed("offer_options", "Your visit is set for today at 11:00.", flow)


def test_a_time_repeated_back_as_settled_is_a_booking():
    """"Normal appointment at 11:00 today it is, Nadia" — and the customer, reasonably,
    stopped talking."""
    flow = _flow(offer_options=("calendar.find_slots",))

    assert _blamed("offer_options", "Normal appointment at 11:00 today it is, Nadia.",
                   flow)


def test_the_flat_claims_are_caught():
    flow = _flow(offer_options=("calendar.find_slots",))

    for said in ("That's booked for you.", "You're booked in for Tuesday.",
                 "I've booked that.", "You're all set — your appointment is Tuesday."):
        assert _blamed("offer_options", said, flow), said


def test_all_set_on_its_own_is_deliberately_not_a_claim():
    """A concession, made against real data and worth stating rather than hiding.

    "You're all set" is ordinary English for "this bit is done", and five of eight
    verdicts in an accounting run were steps saying "you're all set, your record is open"
    — which was true. So it only counts alongside a time or an appointment.

    What that gives up: a step that says a bare "you're all set" to somebody expecting a
    booking, and nothing else. The closing gate catches that one anyway — a last step
    cannot sign off with its own tools uncalled — so the loss is a mid-flow step being
    vague, which is a smell rather than a lie."""
    flow = _flow(offer_options=("calendar.find_slots",))

    assert not _blamed("offer_options", "You're all set.", flow)
    assert not _blamed("offer_options", "You're all set — your record is open.", flow)


def test_a_node_that_has_the_tool_and_did_not_call_it_is_still_caught():
    """The one the old check could not see at all. It asked "does this node have the
    tool" and stopped there, so a booking step that said "that's booked" without booking
    anything went unreported — which is the exact failure the closing gate exists for and
    the commonest failure mode in the literature."""
    flow = _flow(book=("calendar.create_appointment",))

    assert _blamed("book", "That's booked for Tuesday at 11:00.", flow)


def test_the_same_claim_is_fine_when_the_booking_actually_happened():
    flow = _flow(book=("calendar.create_appointment",))

    assert not _blamed("book", "That's booked for Tuesday at 11:00.", flow,
                       appointments=1)


def test_a_message_claimed_and_not_sent():
    flow = _flow(booking=("sms.send",))

    assert _blamed("booking", "I've sent you a text with the details.", flow)
    assert not _blamed("booking", "I've sent you a text with the details.", flow, texts=1)


def test_a_handover_claimed_and_not_made():
    flow = _flow(warranty_handover=("escalate.raise",))

    assert _blamed("warranty_handover", "I've passed this to the technician.", flow)
    assert not _blamed("warranty_handover", "I've passed this to the technician.", flow,
                       escalations=1)


# ---- what it must not ---------------------------------------------------


def test_a_step_that_explicitly_refuses_to_claim_it():
    """Verbatim from an accounting run. The step was being careful and correct, and was
    reported as having lied because the sentence contained the word "booked"."""
    flow = _flow(pick_time=("ticket.set_fields",))

    assert not _blamed(
        "pick_time",
        "I've got your choice noted — March 16 at 10:00 AM. I won't say you're booked "
        "just yet, because that confirmation happens in the next step, but I'm passing "
        "it through.",
        flow,
    )


def test_offering_to_get_something_booked_is_not_a_claim():
    """"I'd be glad to get that booked for you" is an offer. Every wording of a subject
    survives inside a sentence that does not assert it — the same mistake `must_not_say`
    made with "refund" and with "scrub"."""
    flow = _flow(identify=("crm.lookup_by_phone",))

    assert not _blamed("identify", "I'd be glad to get that booked for you. First, "
                                   "could I get your phone number?", flow)


def test_naming_yourself_the_booking_assistant_is_not_a_claim():
    flow = _flow(greeting=("ticket.set_fields",))

    assert not _blamed("greeting", "Hello! I'm the booking assistant at Chen & "
                                   "Associates CPA.", flow)


def test_saying_it_is_not_booked_yet_is_not_a_claim():
    flow = _flow(offer_options=("calendar.find_slots",))

    assert not _blamed("offer_options", "Nothing is booked yet — the next step does "
                                        "that.", flow)


def test_asking_whether_they_want_it_booked_is_not_a_claim():
    flow = _flow(offer_options=("calendar.find_slots",))

    assert not _blamed("offer_options", "Would you like me to get that booked?", flow)


# ---- no more zipping ----------------------------------------------------


def test_the_words_are_read_off_the_step_that_said_them():
    """It used to zip the speaking steps against the transcript's agent lines and trust
    the two to stay in step. One turn that joins a step's parting words to the next
    step's answer is one transcript line for two speaking steps — and every pairing after
    it is silently wrong, so the wrong node gets blamed. Blaming the wrong node is worse
    than not looking."""
    flow = _flow(offer_options=("calendar.find_slots",),
                 service_choice=("ticket.set_fields",))
    result = FakeResult(
        steps=[FakeStep(node="offer_options", text="Here are three times."),
               FakeStep(node="service_choice", text="Your visit is set for 11:00.")],
        # One line for two speaking steps — exactly what broke the old pairing.
        transcript=[("agent", "Here are three times. Your visit is set for 11:00.")],
    )

    blamed = [v.where for v in diagnose._spoke_out_of_turn(result, flow)]

    assert blamed == ["service_choice"]


# ---- a node speaks after it works, not during ----------------------------


def test_the_work_may_happen_on_an_earlier_call_of_the_same_step():
    """`book` calls the diary on its first model call, sends the messages on its second,
    and says "you're all set" on its third — by which point that one call has changed
    nothing. Judged per call, the sentence is a lie; judged over the step, it is exactly
    true. Seven of ten verdicts in a real run were this, which is the same false-positive
    shape the substring detector had, arriving by a different route."""
    flow = _flow(book=("calendar.create_appointment", "sms.send"))
    result = FakeResult(steps=[
        FakeStep(node="book", text="", delta={"appointments": 1}),
        FakeStep(node="book", text="", delta={"texts": 1}),
        FakeStep(node="book", text="You're all set, Marcus.", delta={}),
    ])

    assert [v.because for v in diagnose._spoke_out_of_turn(result, flow)] == []


def test_a_later_step_does_not_cover_an_earlier_one_s_claim():
    """Only what has already happened counts. A step that announces a booking and *then*
    makes it still told the customer something untrue at the moment it said it — and in
    the runs where this matters, the booking never came."""
    flow = _flow(offer_options=("calendar.find_slots",),
                 book=("calendar.create_appointment",))
    result = FakeResult(steps=[
        FakeStep(node="offer_options", text="That's booked for Tuesday.", delta={}),
        FakeStep(node="book", text="", delta={"appointments": 1}),
    ])

    assert [v.where for v in diagnose._spoke_out_of_turn(result, flow)] == ["offer_options"]


# ---- the gap that is itself a diagnosis ----------------------------------


def _result(id_: str, passed: bool, blamed: str = ""):
    verdicts = [diagnose.Verdict(diagnose.MODEL, "went the other way", blamed)] if blamed \
        else []
    return SimpleNamespace(id=id_, passed=passed, verdicts=verdicts, steps=[], smells=[])


def test_a_node_that_passes_alone_and_fails_in_the_flow_points_upstream():
    """The most useful thing a suite can say, and no other comparison gives it. The node
    judges correctly when handed the right ticket, so the fault is a step before it that
    learned something and wrote nothing down."""
    said = diagnose.upstream_gaps([
        _result("node.warranty_check.complaint", True),
        _result("node.warranty_check.other", True),
        _result("complaint_as_warranty", False, blamed="warranty_check"),
    ])

    assert "warranty_check" in said
    assert "look upstream" in said


def test_a_node_that_fails_alone_too_is_not_an_upstream_problem():
    """It is simply wrong, and the fix is in that node's own rules."""
    said = diagnose.upstream_gaps([
        _result("node.warranty_check.complaint", False),
        _result("complaint_as_warranty", False, blamed="warranty_check"),
    ])

    assert said == ""


def test_nothing_is_said_when_there_are_no_node_scenarios():
    """Which is its own signal — a suite with no node scenarios cannot make this
    distinction at all, and dental shipped thirteen scenarios without one."""
    assert diagnose.upstream_gaps([_result("booked", False, blamed="book")]) == ""

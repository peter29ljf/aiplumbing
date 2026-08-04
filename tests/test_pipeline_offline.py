"""End-to-end pipeline test driven by a fake LLM — no API key, no tokens.

The point is to prove the agent loop, orchestrator, handoff and assertion chain are wired
correctly. Actual prompt quality is exercised by scenarios/ and the self-healing loop,
which is a separate concern.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

import pytest

from plumbing import agent_registry, config
from plumbing.orchestrator import Orchestrator
from plumbing.testkit import assertions
from plumbing.tools.registry import ToolContext
from plumbing.world import World

WORKDAY = "2026-08-05T10:00:00-07:00"


# ======================================================================
# Fake LLM
# ======================================================================


@dataclass
class _Function:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Function
    type: str = "function"


@dataclass
class _Message:
    content: str | None = None
    tool_calls: list[_ToolCall] = field(default_factory=list)


class FakeLLM:
    """Scripted playback. The agent role yields tool calls or text; customer yields JSON."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {role: list(items) for role, items in script.items()}
        self.cfg = config.llm_config()
        self.usage = _FakeUsage()
        self.seen: list[str] = []

    def limit(self, name: str, default: int) -> int:
        return int(self.cfg.get("limits", {}).get(name, default))

    def _next(self, role: str) -> Any:
        queue = self.script.get(role) or []
        if not queue:
            raise AssertionError(f"Script for role '{role}' is exhausted (consumed: {self.seen})")
        item = queue.pop(0)
        self.seen.append(f"{role}:{_label(item)}")
        return item

    def chat(self, role: str, messages, tools=None, tool_choice=None, response_format=None):
        item = self._next(role)
        if isinstance(item, str):
            return _Message(content=item)
        calls = [
            _ToolCall(id=f"call_{index}", function=_Function(name=name, arguments=json.dumps(args)))
            for index, (name, args) in enumerate(item)
        ]
        return _Message(content=None, tool_calls=calls)

    def chat_text(self, role: str, messages) -> str:
        return str(self._next(role))

    def chat_json(self, role: str, messages, retries: int = 2) -> dict[str, Any]:
        item = self._next(role)
        return item if isinstance(item, dict) else json.loads(str(item))


class _FakeUsage:
    def as_dict(self) -> dict[str, Any]:
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _label(item: Any) -> str:
    if isinstance(item, list):
        return ",".join(name for name, _ in item)
    if isinstance(item, dict):
        return "json"
    return str(item)[:20]


# ======================================================================
# Wiring
# ======================================================================


def build(script: dict[str, list[Any]], scenario: dict[str, Any]):
    llm = FakeLLM(script)
    world = World(now=scenario.get("world", {}).get("now", WORKDAY), overrides=scenario.get("world", {}))
    ctx = ToolContext(world=world, scenario=dict(scenario))
    cfg = config.agents_config()
    agents = agent_registry.build_all(llm, cfg)

    def customer_sim(agent_message: str | None) -> dict[str, Any]:
        return llm.chat_json("customer", [])

    orchestrator = Orchestrator(agents, "intake", llm, ctx, customer_sim)
    return orchestrator, world, llm


# ======================================================================
# Cases
# ======================================================================


def test_handoff_path_end_to_end():
    """intake creates a ticket, looks up the customer, advances state, records fields,
    hands off to small_job, and the stub closes."""
    scenario = {
        "id": "offline_handoff",
        "world": {"now": WORKDAY},
        "customer": {},
        "expect": {
            "final_status": "Needs Assessment",
            "handoff_to": "small_job",
            "must_call": ["ticket.create", "crm.lookup_by_phone", "handoff.transfer"],
            "must_not_call": ["payment.send_deposit_link"],
        },
    }
    script = {
        "customer": [
            {"text": "my kitchen is leaking", "ended": False},
            {"text": "604-555-0101", "ended": False},
            {"text": "joint under the sink is dripping, not urgent", "ended": False},
        ],
        "agent": [
            [("ticket_create", {})],
            "Hello. May I have your phone number? I use it to look up your service history.",
            [
                ("crm_lookup_by_phone", {"phone": "604-555-0101"}),
                ("ticket_update_status", {"ticket_id": "TK-0001", "status": "Phone Verified"}),
                ("ticket_update_status", {"ticket_id": "TK-0001", "status": "Customer Identified"}),
            ],
            "Thanks Linda, I found your record. What is happening this time?",
            [
                ("ticket_update_status", {"ticket_id": "TK-0001", "status": "Needs Assessment"}),
                ("ticket_set_fields", {"ticket_id": "TK-0001", "fields": {"issue": "sink leak"}}),
                ("rules_get_job_sizing", {}),
                (
                    "handoff_transfer",
                    {"to_agent": "small_job", "reason": "small repair", "summary": "Linda Zhang, sink leak"},
                ),
            ],
            # small_job stub picks it up
            [("ticket_get", {"ticket_id": "TK-0001"})],
            [("conversation_end", {"reason": "stub_handoff_received: small_job"})],
        ],
    }

    orchestrator, world, _ = build(script, scenario)
    result = orchestrator.run()

    assert result.ended_by == "agent", result.end_reason
    assert result.agents_involved == ["intake", "small_job"]

    snapshot = world.snapshot()
    assert snapshot["handoffs"][0]["to_agent"] == "small_job"
    assert snapshot["tickets"]["TK-0001"]["status"] == "Needs Assessment"

    checks = assertions.evaluate(scenario, result, snapshot, world.tool_log)
    failed = [c for c in checks if not c.passed]
    assert not failed, [c.detail for c in failed]


def test_refuse_phone_path_closes_without_sms():
    """Customer withholds their number: general consultation, closed, no record, no booking."""
    scenario = {
        "id": "offline_refuse_phone",
        "world": {"now": WORKDAY},
        "customer": {},
        "expect": {
            "final_status": "Closed",
            "no_handoff": True,
            "sms_count": 0,
            "customer_created": False,
            "must_not_call": ["crm.create_customer", "calendar.create_appointment"],
        },
    }
    script = {
        "customer": [
            {"text": "how much is a call-out?", "ended": False},
            {"text": "I would rather not leave my number", "ended": False},
            {"text": "alright, I will think about it", "ended": False},
        ],
        "agent": [
            [("ticket_create", {})],
            "May I have your phone number? I use it to look up service history and warranty.",
            [
                ("ticket_update_status", {"ticket_id": "TK-0001", "status": "General Consultation"}),
                ("rules_get_standard_service_fee", {}),
            ],
            "Without a number I cannot book or dispatch. A standard call-out starts at CAD 100.",
            [
                ("ticket_update_status", {"ticket_id": "TK-0001", "status": "Closed"}),
                ("conversation_end", {"reason": "general enquiry ended"}),
            ],
        ],
    }

    orchestrator, world, _ = build(script, scenario)
    result = orchestrator.run()

    assert result.ended_by == "agent"
    snapshot = world.snapshot()
    checks = assertions.evaluate(scenario, result, snapshot, world.tool_log)
    failed = [c for c in checks if not c.passed]
    assert not failed, [c.detail for c in failed]


def test_assertions_catch_a_violating_agent():
    """When an agent violates a gate (dispatch before deposit), the assertions must fail —
    otherwise the self-healing loop has no signal."""
    scenario = {
        "id": "offline_violation",
        "world": {"now": WORKDAY},
        "customer": {},
        "expect": {"final_status": "Closed"},
    }
    script = {
        "customer": [{"text": "a pipe burst!", "ended": False}, {"text": "ok", "ended": True}],
        "agent": [
            [("ticket_create", {})],
            [
                (
                    "handoff_transfer",
                    {"to_agent": "emergency", "reason": "burst pipe", "summary": "burst pipe"},
                )
            ],
            # emergency stub oversteps: dispatch without a deposit
            [
                (
                    "calendar_create_appointment",
                    {
                        "ticket_id": "TK-0001",
                        "kind": "emergency",
                        "phone": "+16045550101",
                        "start": WORKDAY,
                        "address": "5900 No. 3 Rd, Richmond",
                        "description": "burst pipe",
                        "technician_id": "t_li",
                    },
                )
            ],
            "That is arranged for you.",
        ],
    }

    orchestrator, world, _ = build(script, scenario)
    orchestrator.run()

    snapshot = world.snapshot()
    assert any(v["kind"] == "dispatch_before_deposit" for v in snapshot["violations"])

    checks = assertions.evaluate(scenario, _ConversationStub(), snapshot, world.tool_log)
    violation_check = next(c for c in checks if c.name == "no_rule_violations")
    assert not violation_check.passed
    assert "dispatch_before_deposit" in violation_check.detail


@dataclass
class _ConversationStub:
    ended_by: str = "agent"
    end_reason: str = ""
    error: str = ""
    agents_involved: list = field(default_factory=list)


def test_turn_limit_is_reported_as_failure():
    """An agent that never closes must be marked failed, not silently allowed to finish."""
    scenario = {"id": "offline_loop", "world": {"now": WORKDAY}, "customer": {}, "expect": {}}
    turns = config.llm_config()["limits"]["max_conversation_turns"]
    script = {
        "customer": [{"text": "hello?", "ended": False}] * (turns + 5),
        "agent": ["Is there anything else I can help with?"] * (turns + 5),
    }
    orchestrator, world, _ = build(script, scenario)
    result = orchestrator.run()

    assert result.ended_by == "turn_limit"
    checks = assertions.evaluate(scenario, result, world.snapshot(), world.tool_log)
    stuck = next(c for c in checks if c.name == "conversation_terminated_cleanly")
    assert not stuck.passed


def test_tool_whitelist_blocks_cross_agent_access():
    """intake has no payment permission; calling it must be refused and recorded."""
    scenario = {"id": "offline_whitelist", "world": {"now": WORKDAY}, "customer": {}, "expect": {}}
    script = {
        "customer": [{"text": "I want a refund", "ended": False}, {"text": "never mind", "ended": True}],
        "agent": [
            [("ticket_create", {})],
            [("payment_refund_deposit", {"ticket_id": "TK-0001"})],
            "Sorry, I need a colleague to handle that.",
        ],
    }
    orchestrator, world, _ = build(script, scenario)
    orchestrator.run()

    assert any(v["kind"] == "unknown_tool" for v in world.snapshot()["violations"])


@pytest.mark.parametrize("agent_name", ["intake", "small_job", "large_job", "emergency", "warranty"])
def test_every_agent_assembles(agent_name):
    """Every agent assembles and its allow-list resolves — catches broken config edits."""
    llm = FakeLLM({})
    agent = agent_registry.build_agent(agent_name, llm)
    assert agent.spec.system_prompt.strip()
    assert agent.spec.tools
    for target in agent.spec.handoff_to:
        assert target in config.agents_config()["agents"]


def test_agent_finishes_the_ticket_after_the_customer_leaves():
    """A customer hanging up does not finish the job.

    A booked repair still has a technician to hear back from and a ticket to close, and in
    real life that happens after the conversation ends, not during it.
    """
    scenario = {
        "id": "offline_wrapup",
        "world": {"now": WORKDAY},
        "customer": {},
        "expect": {"final_status": "Closed"},
    }
    script = {
        "customer": [
            {"text": "my kitchen is leaking", "ended": False},
            {"text": "thanks, see you then", "ended": True},   # customer leaves here
        ],
        "agent": [
            [("ticket_create", {})],
            "Booked for tomorrow morning. You don't need to stay online.",
            # Everything below happens with nobody on the other end
            [("clock_advance", {"minutes": 1440, "reason": "waiting on the technician"})],
            [
                ("ticket_update_status", {"ticket_id": "TK-0001", "status": "Closed"}),
                ("conversation_end", {"reason": "technician reported the work done"}),
            ],
        ],
    }

    orchestrator, world, _ = build(script, scenario)
    result = orchestrator.run()

    assert result.ended_by == "customer"
    assert world.snapshot()["tickets"]["TK-0001"]["status"] == "Closed"
    # The clock moved on after the customer had gone
    assert world.now().hour == 10 and world.now().day == 6


def test_wrapup_does_not_run_forever_when_the_agent_never_finishes():
    """An agent that keeps talking to nobody must still be stopped and reported."""
    scenario = {"id": "offline_wrapup_stuck", "world": {"now": WORKDAY},
                "customer": {}, "expect": {}}
    turns = config.llm_config()["limits"].get("max_wrapup_turns", 8)
    script = {
        "customer": [
            {"text": "hello", "ended": False},
            {"text": "bye", "ended": True},
        ],
        "agent": ["Booked."] + ["Is there anything else?"] * (turns + 3),
    }
    orchestrator, world, _ = build(script, scenario)
    result = orchestrator.run()

    assert result.ended_by == "customer"
    assert "did not finish the ticket" in result.end_reason


def test_a_scheduled_followup_wakes_the_agent_and_finishes_the_job():
    """Parking a job with a follow-up for tomorrow is correct, not stalling.

    In production a scheduler wakes the agent when it falls due. The orchestrator plays
    that scheduler, so a test can watch a whole job finish rather than stopping at "parked".
    """
    scenario = {"id": "offline_followup", "world": {"now": WORKDAY},
                "customer": {}, "expect": {}}
    # intake has no scheduling tools — it never puts a job in a technician's hands — so
    # the job has to reach small_job before anything can be parked.
    script = {
        "customer": [
            {"text": "my kitchen is leaking", "ended": False},
            {"text": "great, thanks", "ended": True},
        ],
        "agent": [
            [("ticket_create", {})],
            [("handoff_transfer", {"to_agent": "small_job", "reason": "small repair",
                                   "summary": "kitchen leak"})],
            "Booked. A technician will attend tomorrow.",
            # Wrap-up: park it with a follow-up and stop — the right thing to do
            [
                ("schedule_create_followup", {
                    "ticket_id": "TK-0001", "in_hours": 24,
                    "purpose": "check_in", "note": "collect the technician's outcome"}),
                ("conversation_end", {"reason": "parked pending the technician"}),
            ],
            # The scheduler fires a day later and the agent finishes the job
            [
                ("ticket_update_status", {"ticket_id": "TK-0001", "status": "Closed"}),
                ("schedule_mark_done", {"followup_id": "FU-0001", "outcome": "work completed"}),
                ("conversation_end", {"reason": "technician reported the work done"}),
            ],
        ],
    }

    orchestrator, world, _ = build(script, scenario)
    result = orchestrator.run()

    assert world.snapshot()["tickets"]["TK-0001"]["status"] == "Closed"
    assert world.followups[0]["status"] == "done"
    # The clock really moved to when the follow-up fell due
    assert world.now().day == 6
    assert any("[scheduler]" in e["text"] for e in result.transcript.entries)


def test_scheduler_does_not_fire_forever():
    """An agent that reschedules itself endlessly must still be stopped."""
    scenario = {"id": "offline_followup_loop", "world": {"now": WORKDAY},
                "customer": {}, "expect": {}}
    firings = config.llm_config()["limits"].get("max_followup_firings", 3)
    reschedule = [
        ("schedule_create_followup", {"ticket_id": "TK-0001", "in_hours": 24,
                                      "purpose": "check_in", "note": "again"}),
        ("conversation_end", {"reason": "still waiting"}),
    ]
    script = {
        "customer": [{"text": "hi", "ended": False}, {"text": "bye", "ended": True}],
        "agent": [
            [("ticket_create", {})],
            [("handoff_transfer", {"to_agent": "small_job", "reason": "small repair",
                                   "summary": "leak"})],
            "Booked.",
        ] + [reschedule] * (firings + 4),
    }
    orchestrator, world, _ = build(script, scenario)
    orchestrator.run()

    # It stopped rather than looping: some follow-ups are left unfired
    assert any(f["status"] == "scheduled" for f in world.followups)


def test_a_broken_customer_simulator_is_reported_as_a_harness_error():
    """A flaky simulator must not be mistaken for the customer ending the conversation.

    Dressed up as a normal ending, harness noise becomes a verdict about the agent, and
    doctor then rewrites a prompt that was never wrong.
    """
    scenario = {"id": "offline_sim_break", "world": {"now": WORKDAY},
                "customer": {}, "expect": {}}
    llm = FakeLLM({"customer": [{"text": "hello", "ended": False}],
                   "agent": [[("ticket_create", {})], "How can I help?"]})
    world = World(now=WORKDAY)
    ctx = ToolContext(world=world, scenario=dict(scenario))
    agents = agent_registry.build_all(llm, config.agents_config())

    calls = {"n": 0}

    def customer_sim(_message):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"text": "my kitchen is leaking", "ended": False}
        return {"text": "", "ended": True, "error": True,
                "reason": "Customer simulator failed: bad JSON"}

    result = Orchestrator(agents, "intake", llm, ctx, customer_sim).run()

    assert result.ended_by == "error"
    assert "simulator failed" in result.error

    checks = assertions.evaluate(scenario, result, world.snapshot(), world.tool_log)
    assert not checks[0].passed
    assert "Run failed" in checks[0].detail


# ======================================================================
# Customer simulator reply parsing
#
# Strict JSON at temperature 0.9 across a forty-turn conversation cost a quarter of
# end-to-end runs. Plain text has nothing to malform — but the parser has to be
# forgiving about how the model decorates it.
# ======================================================================


@pytest.mark.parametrize(
    "raw,text,ended",
    [
        # The ordinary case: just words
        ("yeah it's still dripping, about a bucket overnight",
         "yeah it's still dripping, about a bucket overnight", False),
        # Marker on its own line, with a reason
        ("great, thanks — see you Tuesday\n[END] appointment booked",
         "great, thanks — see you Tuesday", True),
        # Marker with no reason given
        ("ok bye\n[END]", "ok bye", True),
        # Lowercase marker
        ("that's all\n[end] done", "that's all", True),
        # Marker tacked onto the end of the sentence instead of its own line
        ("thanks, bye [END] nothing else needed", "thanks, bye", True),
        # Model wrapped it in a code fence
        ("```\nsure, Tuesday works\n```", "sure, Tuesday works", False),
        # Multi-line message
        ("first line\nsecond line", "first line\nsecond line", False),
    ],
)
def test_customer_reply_parsing(raw, text, ended):
    from plumbing.sim.customer import parse_reply

    got_text, got_ended, _ = parse_reply(raw)
    assert got_text == text
    assert got_ended is ended


def test_ending_with_no_words_still_says_goodbye():
    """An [END] on its own would otherwise produce an empty customer turn."""
    from plumbing.sim.customer import parse_reply

    text, ended, reason = parse_reply("[END] got what I needed")
    assert ended is True
    assert text.strip()
    assert reason == "got what I needed"


def test_a_reply_that_is_only_whitespace_does_not_end_the_conversation():
    from plumbing.sim.customer import parse_reply

    text, ended, _ = parse_reply("   \n  ")
    assert ended is False
    assert text == "..."


# ======================================================================
# Repeat-based verdicts
#
# The customer simulator samples at high temperature, so one run is not evidence: the
# same code run twice produced five failures and eight, with only four in common. What
# this logic decides is which failures doctor is allowed to rewrite a prompt for.
# ======================================================================


def _verdict(*passed: bool):
    from plumbing.testkit.loop import ScenarioVerdict
    from plumbing.testkit.runner import ScenarioResult

    entry = ScenarioVerdict("s")
    for ok in passed:
        entry.runs.append(ScenarioResult(scenario_id="s", suite="j", description="",
                                         passed=ok))
    return entry


def test_all_runs_passing_is_a_pass():
    assert _verdict(True, True).verdict == "pass"
    assert _verdict(True, True).passed is True
    assert _verdict(True, True).actionable is False


def test_all_runs_failing_is_a_real_failure():
    entry = _verdict(False, False)
    assert entry.verdict == "fail"
    assert entry.actionable is True          # doctor may act on this


def test_a_mixed_result_is_flaky_and_never_reaches_doctor():
    """The whole point: a prompt that was only unlucky must not be rewritten."""
    entry = _verdict(True, False)
    assert entry.verdict == "flaky"
    assert entry.passed is False             # not counted as passing
    assert entry.actionable is False         # but not handed to doctor either

    assert _verdict(False, True).verdict == "flaky"
    assert _verdict(True, False, True).verdict == "flaky"


def test_doctor_reads_a_failing_run_not_a_passing_one():
    """Handing doctor the run that happened to succeed would tell it nothing."""
    from plumbing.testkit.runner import ScenarioResult
    from plumbing.testkit.loop import ScenarioVerdict

    entry = ScenarioVerdict("s")
    entry.runs.append(ScenarioResult(scenario_id="s", suite="j", description="",
                                     passed=True, end_reason="the good one"))
    entry.runs.append(ScenarioResult(scenario_id="s", suite="j", description="",
                                     passed=False, end_reason="the bad one"))
    assert entry.representative.end_reason == "the bad one"


def test_a_single_run_still_yields_a_usable_verdict():
    """--repeat 1 keeps working; it simply cannot detect flakiness."""
    assert _verdict(True).verdict == "pass"
    assert _verdict(False).verdict == "fail"


def test_verdict_summary_reports_the_split():
    assert _verdict(True, False).summary == "1/2 passed"
    assert _verdict(False, False, False).summary == "0/3 passed"


def test_a_failure_is_confirmed_with_extra_runs_before_it_is_believed():
    """With repeat=2, a genuinely 50/50 scenario comes out "fail" a quarter of the time —
    often enough to send doctor after a prompt that was only unlucky. Re-running just the
    failures costs little, because there are few of them.
    """
    from plumbing.testkit import loop as loop_mod

    scenario = {"id": "coinflip", "customer": {}, "expect": {}}
    calls = {"n": 0}

    def fake_run(spec, llm, *, run_judge=True):
        from plumbing.testkit.runner import ScenarioResult
        calls["n"] += 1
        # Fails the first two runs, passes afterwards — exactly the case repeat=2 misreads
        return ScenarioResult(scenario_id=spec["id"], suite="j", description="",
                              passed=calls["n"] > 2)

    original = loop_mod.run_scenario
    try:
        loop_mod.run_scenario = fake_run
        verdicts = loop_mod.run_suite(
            [scenario], FakeLLM({}), pathlib.Path("/tmp/does-not-matter"),
            run_judge=False, workers=1, repeat=2,
        )
    finally:
        loop_mod.run_scenario = original

    entry = verdicts["coinflip"]
    assert calls["n"] == 4, "the failure should have been re-run before being believed"
    assert entry.verdict == "flaky"      # not "fail"
    assert entry.actionable is False     # so doctor is never handed it


def test_reliably_passing_scenarios_pay_nothing_for_confirmation():
    from plumbing.testkit import loop as loop_mod

    scenario = {"id": "steady", "customer": {}, "expect": {}}
    calls = {"n": 0}

    def fake_run(spec, llm, *, run_judge=True):
        from plumbing.testkit.runner import ScenarioResult
        calls["n"] += 1
        return ScenarioResult(scenario_id=spec["id"], suite="j", description="", passed=True)

    original = loop_mod.run_scenario
    try:
        loop_mod.run_scenario = fake_run
        loop_mod.run_suite([scenario], FakeLLM({}), pathlib.Path("/tmp/x"),
                           run_judge=False, workers=1, repeat=2)
    finally:
        loop_mod.run_scenario = original

    assert calls["n"] == 2, "a passing scenario must not be re-run"


def test_a_genuine_failure_survives_confirmation():
    from plumbing.testkit import loop as loop_mod

    scenario = {"id": "broken", "customer": {}, "expect": {}}

    def fake_run(spec, llm, *, run_judge=True):
        from plumbing.testkit.runner import ScenarioResult
        return ScenarioResult(scenario_id=spec["id"], suite="j", description="", passed=False)

    original = loop_mod.run_scenario
    try:
        loop_mod.run_scenario = fake_run
        verdicts = loop_mod.run_suite([scenario], FakeLLM({}), pathlib.Path("/tmp/x"),
                                      run_judge=False, workers=1, repeat=2)
    finally:
        loop_mod.run_scenario = original

    assert verdicts["broken"].verdict == "fail"
    assert verdicts["broken"].actionable is True


def test_the_three_counts_add_up_to_the_number_of_scenarios():
    """Flaky is not passing. Counting it as such made the summary read
    "4 passing, 0 failing, 3 flaky (of 4)"."""
    from plumbing.testkit.loop import LoopReport

    report = LoopReport(suite="j", started_at="", scenarios=["a", "b", "c", "d"],
                        final_failures=["a"], flaky=["b", "c"])
    total = len(report.scenarios)
    passing = total - len(report.final_failures) - len(report.flaky)
    assert passing == 1
    assert passing + len(report.final_failures) + len(report.flaky) == total


# ======================================================================
# Failure classification
#
# One question decides everything here: could doctor fix this by editing a prompt?
# Handing it anything else produces a confident, useless edit.
# ======================================================================


def _result_with(checks):
    from plumbing.testkit.runner import ScenarioResult

    return ScenarioResult(scenario_id="s", suite="j", description="", passed=False,
                          checks=checks)


def test_a_broken_simulator_is_harness_not_agent():
    from plumbing.testkit import assertions

    scenario = {"id": "x", "customer": {}, "expect": {}}
    result = _ConversationStub(ended_by="error",
                               error="Customer simulator failed: bad reply")
    checks = assertions.evaluate(scenario, result, World(now=WORKDAY).snapshot(), [])
    assert checks[0].source == "harness"


def test_a_crash_is_framework_not_harness():
    from plumbing.testkit import assertions

    scenario = {"id": "x", "customer": {}, "expect": {}}
    result = _ConversationStub(ended_by="error", error="KeyError: 'ticket_id'")
    checks = assertions.evaluate(scenario, result, World(now=WORKDAY).snapshot(), [])
    assert checks[0].source == "framework"


def test_a_fired_hard_gate_is_framework():
    """Every illegal transition seen so far has been a missing edge, not a rogue agent."""
    from plumbing.testkit import assertions

    world = World(now=WORKDAY)
    world.record_violation("illegal_ticket_transition", "cannot go there", "ticket.update_status")
    checks = assertions.evaluate({"id": "x", "customer": {}, "expect": {}},
                                 _ConversationStub(), world.snapshot(), [])
    gate = next(c for c in checks if c.name == "no_rule_violations")
    assert gate.passed is False
    assert gate.source == "framework"


def test_an_ordinary_expectation_miss_is_the_agent():
    from plumbing.testkit import assertions

    checks = assertions.evaluate(
        {"id": "x", "customer": {}, "expect": {"must_call": ["sms.send"]}},
        _ConversationStub(), World(now=WORKDAY).snapshot(), [])
    miss = next(c for c in checks if c.name.startswith("must_call"))
    assert miss.passed is False
    assert miss.source == "agent"


def test_doctor_is_only_offered_agent_failures():
    from plumbing.testkit.loop import ScenarioVerdict

    def verdict_for(source_check):
        entry = ScenarioVerdict("s")
        entry.runs = [_result_with([source_check]), _result_with([source_check])]
        return entry

    framework = {"name": "no_rule_violations", "passed": False,
                 "detail": "gate fired", "source": "framework"}
    agent = {"name": "must_call:sms.send", "passed": False,
             "detail": "never called", "source": "agent"}
    harness = {"name": "run_completed", "passed": False,
               "detail": "simulator failed", "source": "harness"}

    assert verdict_for(agent).actionable is True
    assert verdict_for(framework).actionable is False
    assert verdict_for(harness).actionable is False


def test_the_worst_source_wins_when_a_scenario_has_several():
    """A framework block also makes the agent miss everything that came after it.
    Fixing the framework first is the only order that makes sense."""
    from plumbing.testkit.loop import ScenarioVerdict

    entry = ScenarioVerdict("s")
    mixed = _result_with([
        {"name": "must_call:sms.send", "passed": False, "detail": "", "source": "agent"},
        {"name": "no_rule_violations", "passed": False, "detail": "", "source": "framework"},
    ])
    entry.runs = [mixed, mixed]
    assert entry.source == "framework"
    assert entry.actionable is False


def test_heal_skips_framework_failures_and_never_calls_doctor(capsys, tmp_path):
    """End-to-end through heal(): a framework-blocked scenario must reach the report
    without doctor being consulted.

    Waiting for the live suite to produce a framework failure on demand is hoping for a
    coincidence; stubbing one makes the check deterministic.
    """
    from plumbing.testkit import doctor as doctor_mod
    from plumbing.testkit import loop as loop_mod
    from plumbing.testkit.runner import ScenarioResult

    scenario = {"id": "blocked", "suite": "j", "customer": {}, "expect": {}}
    doctor_calls = {"n": 0}

    def fake_run(spec, llm, *, run_judge=True):
        return ScenarioResult(
            scenario_id=spec["id"], suite="j", description="", passed=False,
            checks=[{"name": "no_rule_violations", "passed": False,
                     "source": "framework", "detail": "illegal_ticket_transition"}],
        )

    def fake_propose(*args, **kwargs):
        doctor_calls["n"] += 1
        raise AssertionError("doctor was asked to fix a framework failure")

    original_run, original_propose = loop_mod.run_scenario, doctor_mod.propose
    original_dir = loop_mod.new_run_dir
    try:
        loop_mod.run_scenario = fake_run
        doctor_mod.propose = fake_propose
        loop_mod.new_run_dir = lambda label="run": tmp_path / label
        report = loop_mod.heal([scenario], suite="j", max_repair_rounds=2,
                               run_judge=False, regression=False, workers=1, repeat=2)
    finally:
        loop_mod.run_scenario = original_run
        doctor_mod.propose = original_propose
        loop_mod.new_run_dir = original_dir

    assert doctor_calls["n"] == 0
    assert report.final_failures == ["blocked"]

    out = capsys.readouterr().out
    assert "framework: 1" in out
    assert "Skipping blocked (framework)" in out
    assert "Not something a prompt edit can fix" in out


def test_heal_does_hand_over_a_genuine_agent_failure(tmp_path):
    """The other half: classification must not block doctor from real work."""
    from plumbing.testkit import doctor as doctor_mod
    from plumbing.testkit import loop as loop_mod
    from plumbing.testkit.runner import ScenarioResult

    scenario = {"id": "misbehaving", "suite": "j", "customer": {}, "expect": {}}
    seen = {"n": 0}

    def fake_run(spec, llm, *, run_judge=True):
        return ScenarioResult(
            scenario_id=spec["id"], suite="j", description="", passed=False,
            checks=[{"name": "must_call:sms.send", "passed": False,
                     "source": "agent", "detail": "never called"}],
        )

    def fake_propose(*args, **kwargs):
        seen["n"] += 1
        return None            # no usable patch; enough to prove it was consulted

    original_run, original_propose = loop_mod.run_scenario, doctor_mod.propose
    original_dir = loop_mod.new_run_dir
    try:
        loop_mod.run_scenario = fake_run
        doctor_mod.propose = fake_propose
        loop_mod.new_run_dir = lambda label="run": tmp_path / label
        loop_mod.heal([scenario], suite="j", max_repair_rounds=1,
                      run_judge=False, regression=False, workers=1, repeat=2)
    finally:
        loop_mod.run_scenario = original_run
        doctor_mod.propose = original_propose
        loop_mod.new_run_dir = original_dir

    assert seen["n"] == 1, "doctor should have been offered the agent-class failure"


def test_an_incidental_framework_blip_does_not_mask_a_persistent_agent_failure():
    """The case that exposed this: one run hit an illegal transition, both runs missed a
    required tool call. Judging on the worst source seen anywhere skipped doctor entirely,
    leaving the agent problem that failed every single time unaddressed.
    """
    from plumbing.testkit.loop import ScenarioVerdict
    from plumbing.testkit.runner import ScenarioResult

    agent_miss = {"name": "must_call:phone.call_technician", "passed": False,
                  "detail": "never called", "source": "agent"}
    gate_blip = {"name": "no_rule_violations", "passed": False,
                 "detail": "illegal transition", "source": "framework"}

    entry = ScenarioVerdict("s")
    entry.runs = [
        ScenarioResult(scenario_id="s", suite="j", description="", passed=False,
                       checks=[agent_miss, gate_blip]),          # blip only here
        ScenarioResult(scenario_id="s", suite="j", description="", passed=False,
                       checks=[agent_miss]),
    ]

    names = {f["name"] for f in entry.persistent_failures}
    assert names == {"must_call:phone.call_technician"}
    assert entry.source == "agent"
    assert entry.actionable is True      # doctor gets it, as it should


def test_a_framework_failure_in_every_run_still_blocks_doctor():
    """The rule must not become "ignore framework failures" — only "ignore the flaky ones"."""
    from plumbing.testkit.loop import ScenarioVerdict
    from plumbing.testkit.runner import ScenarioResult

    gate = {"name": "no_rule_violations", "passed": False,
            "detail": "illegal transition", "source": "framework"}
    agent_miss = {"name": "must_call:sms.send", "passed": False,
                  "detail": "never called", "source": "agent"}

    entry = ScenarioVerdict("s")
    entry.runs = [
        ScenarioResult(scenario_id="s", suite="j", description="", passed=False,
                       checks=[gate, agent_miss]),
        ScenarioResult(scenario_id="s", suite="j", description="", passed=False,
                       checks=[gate, agent_miss]),
    ]
    assert entry.source == "framework"
    assert entry.actionable is False


def test_switching_from_a_standard_slot_to_emergency_is_allowed():
    """A customer offered tomorrow who decides they want someone tonight has to reach the
    deposit, which is where the emergency flow now starts."""
    world = World(now=WORKDAY)
    ticket = world.seed_ticket("Awaiting Appointment Selection", "+16045550101")
    world.transition_ticket(ticket.ticket_id, "Deposit Link Sent")
    assert ticket.status == "Deposit Link Sent"

"""Hard assertions: tool call sequence, final world state, closure behaviour.

No semantic judgement here (that is judge.py's job) — only deterministic checks, so the
same transcript always produces the same verdict and doctor gets a stable signal.

Every check carries a `source`, answering one question: **could doctor fix this by editing
a prompt?**

- `harness`   the test rig broke — a simulator or the model failed. Nothing to fix here.
- `framework` the state machine, a hard gate or the tool permissions blocked something.
              A human decides whether the rule or the flow is wrong; no prompt can route
              around it, and every illegal transition seen so far has been a missing edge
              rather than a misbehaving agent.
- `agent`     the agent did the wrong thing. This is the only kind doctor should touch.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from plumbing.orchestrator import ConversationResult


HARNESS = "harness"
FRAMEWORK = "framework"
AGENT = "agent"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    source: str = AGENT

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "source": self.source,
        }


def evaluate(
    scenario: dict[str, Any],
    result: ConversationResult,
    snapshot: dict[str, Any],
    tool_log: list[dict[str, Any]],
) -> list[Check]:
    expect = scenario.get("expect", {}) or {}
    checks: list[Check] = []
    called = [entry["tool"] for entry in tool_log]
    called_ok = [entry["tool"] for entry in tool_log if entry.get("ok")]

    def scoped(pattern: str, only_ok: bool) -> tuple[str, list[str]]:
        """Support "agent:tool.name" so a test can pin an expectation to one agent.

        An intake scenario should not fail because a downstream agent did its own job
        after the handoff — scope the expectation and it measures only what it means to.
        """
        agent, sep, tool_pattern = pattern.partition(":")
        if not sep:
            return pattern, (called_ok if only_ok else called)
        pool = [
            e["tool"]
            for e in tool_log
            if e.get("agent") == agent and (e.get("ok") if only_ok else True)
        ]
        return tool_pattern, pool

    # ---- Did the run itself survive? ---------------------------------
    if result.ended_by == "error":
        source = HARNESS if "simulator failed" in (result.error or "").lower() else FRAMEWORK
        checks.append(Check("run_completed", False, f"Run failed: {result.error}", source))
        return checks
    checks.append(Check("run_completed", True, "Run completed"))

    if result.ended_by in ("turn_limit", "handoff_limit"):
        checks.append(
            Check(
                "conversation_terminated_cleanly",
                False,
                f"Conversation never closed properly and was force-stopped: {result.end_reason}. "
                f"The agent either looped or never reached its closing actions.",
                AGENT,
            )
        )

    # ---- Violations (a hard gate fired) ------------------------------
    if expect.get("allow_violations", False) is not True:
        allowed = set(expect.get("expected_violations", []))
        actual = [v for v in snapshot["violations"] if v["kind"] not in allowed]
        checks.append(
            Check(
                "no_rule_violations",
                not actual,
                "No violations"
                if not actual
                else "Rule hard gates fired: "
                + "; ".join(f"{v['kind']}({v['tool']}) {v['detail'][:80]}" for v in actual),
                FRAMEWORK,
            )
        )

    # ---- Final ticket status -----------------------------------------
    if "final_status" in expect:
        wanted = expect["final_status"]
        actual_status = _active_ticket_status(snapshot)
        checks.append(
            Check(
                "final_status",
                actual_status == wanted,
                f"Expected ticket status '{wanted}', got '{actual_status}'",
            )
        )

    if "final_status_in" in expect:
        wanted = list(expect["final_status_in"])
        actual_status = _active_ticket_status(snapshot)
        checks.append(
            Check(
                "final_status_in",
                actual_status in wanted,
                f"Expected ticket status in {wanted}, got '{actual_status}'",
            )
        )

    # ---- Tool calls --------------------------------------------------
    for pattern in expect.get("must_call", []):
        tool_pattern, pool = scoped(pattern, only_ok=True)
        hit = _matches(pool, tool_pattern)
        checks.append(
            Check(
                f"must_call:{pattern}",
                bool(hit),
                f"Called {hit}" if hit else f"Never successfully called {pattern}",
            )
        )

    for pattern in expect.get("must_not_call", []):
        tool_pattern, pool = scoped(pattern, only_ok=False)
        hit = _matches(pool, tool_pattern)
        checks.append(
            Check(
                f"must_not_call:{pattern}",
                not hit,
                "Not called" if not hit else f"Called {hit}, which was forbidden",
            )
        )

    if "max_tool_calls" in expect:
        limit = int(expect["max_tool_calls"])
        checks.append(
            Check(
                "max_tool_calls",
                len(called) <= limit,
                f"{len(called)} tool calls, limit {limit}",
            )
        )

    if "max_call_rounds" in expect:
        limit = int(expect["max_call_rounds"])
        rounds = {c["round"] for c in snapshot["call_records"]}
        checks.append(
            Check(
                "max_call_rounds",
                len(rounds) <= limit,
                f"{len(rounds)} technician calling rounds, limit {limit}",
            )
        )

    # ---- Handoff -----------------------------------------------------
    handoff_targets = [h["to_agent"] for h in snapshot["handoffs"]]
    if "handoff_to" in expect:
        wanted = expect["handoff_to"]
        wanted_list = wanted if isinstance(wanted, list) else [wanted]
        checks.append(
            Check(
                "handoff_to",
                any(t in wanted_list for t in handoff_targets),
                f"Expected handoff to {wanted_list}, actual: {handoff_targets or 'none'}",
            )
        )
    if expect.get("no_handoff"):
        checks.append(
            Check(
                "no_handoff",
                not handoff_targets,
                "No handoff" if not handoff_targets else f"Handed off to {handoff_targets}, which was forbidden",
            )
        )

    # ---- Messages ----------------------------------------------------
    sms = snapshot["sms_outbox"]
    for purpose in expect.get("sms_purposes_include", []):
        hit = [m for m in sms if m["purpose"] == purpose]
        checks.append(
            Check(
                f"sms_purpose:{purpose}",
                bool(hit),
                f"Sent {len(hit)}" if hit else f"No message sent with purpose {purpose}",
            )
        )

    for purpose in expect.get("sms_purposes_exclude", []):
        hit = [m for m in sms if m["purpose"] == purpose]
        checks.append(
            Check(
                f"sms_purpose_absent:{purpose}",
                not hit,
                "Not sent" if not hit else f"Sent {len(hit)} {purpose} messages, which was forbidden",
            )
        )

    for needle in expect.get("sms_any_contains", []):
        hit = any(needle in m["body"] for m in sms)
        checks.append(
            Check(
                f"sms_contains:{needle}",
                hit,
                "Found in a message" if hit else f"No message contained '{needle}'",
            )
        )

    if "sms_count" in expect:
        wanted = int(expect["sms_count"])
        checks.append(
            Check("sms_count", len(sms) == wanted, f"Expected {wanted} messages, got {len(sms)}")
        )

    if "min_sms_count" in expect:
        wanted = int(expect["min_sms_count"])
        checks.append(
            Check(
                "min_sms_count",
                len(sms) >= wanted,
                f"Expected at least {wanted} messages, got {len(sms)}",
            )
        )

    # ---- Appointments and payments -----------------------------------
    appointments = snapshot["appointments"]
    if "appointment_kind" in expect:
        wanted = expect["appointment_kind"]
        kinds = [a["kind"] for a in appointments]
        checks.append(
            Check(
                "appointment_kind",
                wanted in kinds,
                f"Expected a {wanted} appointment, actual: {kinds or 'none created'}",
            )
        )
    if expect.get("no_appointment"):
        checks.append(
            Check(
                "no_appointment",
                not appointments,
                "No appointment created" if not appointments else f"Created {len(appointments)} appointments, which was forbidden",
            )
        )

    if "payment_status" in expect:
        wanted = expect["payment_status"]
        statuses = [p["status"] for p in snapshot["payments"]]
        checks.append(
            Check(
                "payment_status",
                wanted in statuses,
                f"Expected a payment with status {wanted}, actual: {statuses or 'none'}",
            )
        )

    # ---- Customer record ---------------------------------------------
    if "customer_created" in expect:
        wanted = bool(expect["customer_created"])
        created = bool(snapshot["customers_created"])
        checks.append(
            Check(
                "customer_created",
                created == wanted,
                f"Expected customer record to {'be' if wanted else 'not be'} created; it was {'created' if created else 'not created'}",
            )
        )

    # ---- Escalation --------------------------------------------------
    if "escalated" in expect:
        wanted = bool(expect["escalated"])
        actual_escalated = bool(snapshot["escalations"])
        checks.append(
            Check(
                "escalated",
                actual_escalated == wanted,
                f"Expected escalation to {'happen' if wanted else 'not happen'}; it {'happened' if actual_escalated else 'did not'}",
            )
        )

    return checks


def _active_ticket_status(snapshot: dict[str, Any]) -> str:
    ticket_id = snapshot.get("active_ticket_id")
    tickets = snapshot.get("tickets", {})
    if ticket_id and ticket_id in tickets:
        return tickets[ticket_id]["status"]
    if tickets:
        return next(iter(tickets.values()))["status"]
    return "<no ticket created>"


def _matches(called: list[str], pattern: str) -> list[str]:
    """Supports globs such as 'crm.*'."""
    if "*" in pattern:
        return sorted({c for c in called if fnmatch.fnmatchcase(c, pattern)})
    return [c for c in called if c == pattern][:1]

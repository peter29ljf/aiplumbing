"""Run one scenario: build the world and simulators, play out the conversation, assert, save.

    PYTHONPATH=src python3 -m plumbing.testkit.runner scenarios/intake/xxx.yaml --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from plumbing import agent_registry, config, livestatus
from plumbing.llm import LLM
from plumbing.orchestrator import ConversationResult, Orchestrator
from plumbing.paths import RUNS_DIR
from plumbing.sim import CustomerSim, SupervisorSim, TechnicianSim
from plumbing.testkit import assertions, judge, scenario as scenario_mod
from plumbing.tools.registry import ToolContext
from plumbing.world import World


@dataclass
class ScenarioResult:
    scenario_id: str
    suite: str
    description: str
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    transcript_text: str = ""
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    ended_by: str = ""
    end_reason: str = ""
    agents_involved: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def failures(self) -> list[dict[str, Any]]:
        out = [c for c in self.checks if not c["passed"]]
        out += [
            {
                "name": f"judge:{v['id']}",
                "passed": False,
                "source": "agent",     # the judge only ever rules on what the agent said
                "detail": f"{v.get('question', '')} -> {v['reason']} "
                f"(evidence: {v['evidence'] or 'none'})",
            }
            for v in self.verdicts
            if not v["passed"]
        ]
        return out

    @property
    def failure_source(self) -> str:
        """Where this scenario's failures come from, worst first.

        A scenario blocked by the framework may also show agent-looking symptoms — an
        agent that cannot advance a ticket will miss the calls that follow. Fixing the
        framework first is the only order that makes sense, so the worst source wins.
        """
        sources = {f.get("source", "agent") for f in self.failures}
        for candidate in ("harness", "framework", "agent"):
            if candidate in sources:
                return candidate
        return "agent"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "suite": self.suite,
            "description": self.description,
            "passed": self.passed,
            "ended_by": self.ended_by,
            "end_reason": self.end_reason,
            "agents_involved": self.agents_involved,
            "failure_source": self.failure_source if not self.passed else "",
            "checks": self.checks,
            "verdicts": self.verdicts,
            "failures": self.failures,
            "transcript": self.transcript,
            "tool_log": self.tool_log,
            "snapshot": self.snapshot,
            "usage": self.usage,
            "error": self.error,
        }


def run_scenario(
    scenario: dict[str, Any],
    llm: LLM | None = None,
    *,
    run_judge: bool = True,
) -> ScenarioResult:
    config.reload_all()  # pick up any prompt edits doctor has made
    llm = llm or LLM()

    world_spec = scenario.get("world", {}) or {}
    world = World(
        now=world_spec.get("now", datetime.now().isoformat()),
        overrides=world_spec,
    )

    customer_sim = CustomerSim(llm, scenario)
    technician_sim = TechnicianSim(llm, scenario)
    supervisor_sim = SupervisorSim(llm, scenario)

    # A scenario can pin itself to the agents a particular deployment runs. The `live`
    # suite does, because its whole subject is what happens when the others are off; the
    # `journey` suite does not, because it exists to cover the full system.
    enabled = scenario.get("enabled_agents")
    enabled_set = set(enabled) if enabled else None

    ctx = ToolContext(
        world=world,
        technician_sim=technician_sim,
        supervisor_sim=supervisor_sim,
        scenario=dict(scenario),
        enabled_agents=tuple(enabled) if enabled else None,
    )

    cfg = config.agents_config()
    agents = agent_registry.build_all(llm, cfg, enabled_set)
    if enabled_set:
        agents = {name: agent for name, agent in agents.items() if name in enabled_set}
    entry = scenario.get("entry_agent") or agent_registry.entry_agent_name(cfg)

    # A scenario entering at a non-entry agent is modelling a handoff that already
    # happened; seed the ticket and brief the agent exactly as a real handoff would.
    briefing = ""
    ticket_spec = world_spec.get("ticket") or {}
    if ticket_spec:
        seeded = world.seed_ticket(
            status=ticket_spec.get("status", "Needs Assessment"),
            phone=ticket_spec.get("phone", ""),
            fields=ticket_spec.get("fields") or {},
        )
        summary = ticket_spec.get("summary", "")
        briefing = (
            f"[handoff] A colleague has passed you this ticket.\n"
            f"**Ticket: {seeded.ticket_id}** — it already exists at status "
            f"'{seeded.status}'. Keep using it; do not call ticket.create.\n"
            + (f"Background: {summary}\n" if summary else "")
            + f"Start with ticket.get on {seeded.ticket_id} to see what has been recorded, "
            f"then serve the customer. Do not ask again for anything already on the ticket."
        )

    livestatus.start_run(scenario["id"], scenario.get("suite", ""))
    orchestrator = Orchestrator(agents, entry, llm, ctx, customer_sim, opening_briefing=briefing)
    conversation: ConversationResult = orchestrator.run()

    snapshot = world.snapshot()
    checks = assertions.evaluate(scenario, conversation, snapshot, world.tool_log)

    verdicts: list[dict[str, Any]] = []
    if run_judge and conversation.ended_by != "error":
        livestatus.set_phase("judging")
        verdicts = judge.evaluate(
            llm, scenario, conversation.transcript.as_text(), world.tool_log
        )

    passed = all(c.passed for c in checks) and all(v["passed"] for v in verdicts)
    livestatus.finish_run("pass" if passed else "fail")

    return ScenarioResult(
        scenario_id=scenario["id"],
        suite=scenario.get("suite", ""),
        description=scenario.get("description", ""),
        passed=passed,
        checks=[c.as_dict() for c in checks],
        verdicts=verdicts,
        transcript=conversation.transcript.entries,
        transcript_text=conversation.transcript.as_text(),
        tool_log=world.tool_log,
        snapshot=snapshot,
        ended_by=conversation.ended_by,
        end_reason=conversation.end_reason,
        agents_involved=conversation.agents_involved,
        usage=llm.usage.as_dict(),
        error=conversation.error,
    )


def save_result(result: ScenarioResult, run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{result.scenario_id}.json"
    path.write_text(
        json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def new_run_dir(label: str = "run") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return RUNS_DIR / f"{stamp}-{label}"


def print_result(result: ScenarioResult, verbose: bool = False) -> None:
    mark = "PASS" if result.passed else "FAIL"
    print(f"\n{mark}  [{result.scenario_id}] {result.description}")
    print(f"       ended by: {result.ended_by} ({result.end_reason})")
    print(f"       agents: {' -> '.join(result.agents_involved)}")

    if verbose:
        print("\n--- conversation ---")
        for entry in result.transcript:
            label = {"customer": "Customer", "agent": f"AI[{entry.get('agent','')}]"}.get(
                entry["speaker"], "System"
            )
            print(f"{label}: {entry['text']}")
        print("\n--- tool calls ---")
        for index, entry in enumerate(result.tool_log, 1):
            status = "ok" if entry.get("ok") else "ERR"
            note = "" if entry.get("ok") else f"  <- {entry.get('error','')}"
            print(f"{index:>2}. [{status}] {entry['tool']}{note}")
        print("\n--- final world state ---")
        for ticket_id, ticket in result.snapshot["tickets"].items():
            print(f"  ticket {ticket_id}: {ticket['status']}")
        for message in result.snapshot["sms_outbox"]:
            print(f"  sms[{message['purpose']}] -> {message['to']}: {message['body'][:60]}")

    if result.failures:
        print("\n       failures:")
        for failure in result.failures:
            print(f"       - {failure['name']}: {failure['detail']}")
    print(f"       usage: {result.usage}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a single test scenario")
    parser.add_argument("scenario", help="Path to the scenario YAML")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print the full conversation and tool calls")
    parser.add_argument("--no-judge", action="store_true", help="Skip the LLM judge (saves tokens)")
    parser.add_argument("--save", action="store_true", help="Write the result into runs/")
    args = parser.parse_args(argv)

    spec = scenario_mod.load(args.scenario)
    result = run_scenario(spec, run_judge=not args.no_judge)
    print_result(result, verbose=args.verbose)

    if args.save:
        path = save_result(result, new_run_dir("single"))
        print(f"\nResult saved: {path}")

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())

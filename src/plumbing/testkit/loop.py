"""The self-healing loop: run the suite, let doctor edit prompts, re-run, regress, revert.

    PYTHONPATH=src python3 -m plumbing.testkit.loop --suite intake --max-repair-rounds 5

Regression is the critical step. Doctor will happily hard-code a rule to fix scenario A
and break scenario B doing it. Every patch re-runs the scenarios that already passed, and
anything that breaks them is reverted.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from plumbing.llm import LLM
from plumbing.testkit import doctor, scenario as scenario_mod
from plumbing.testkit.runner import (
    ScenarioResult,
    new_run_dir,
    run_scenario,
    save_result,
)


@dataclass
class RepairRecord:
    scenario_id: str
    round_number: int
    file: str
    reason: str
    outcome: str            # fixed | reverted_scenario_still_failing | reverted_regression | rejected
    regression_broke: list[str] = field(default_factory=list)
    history_path: str = ""


@dataclass
class LoopReport:
    suite: str
    started_at: str
    scenarios: list[str] = field(default_factory=list)
    initial_failures: list[str] = field(default_factory=list)
    final_failures: list[str] = field(default_factory=list)
    repairs: list[RepairRecord] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)


def run_suite(
    scenarios: list[dict[str, Any]],
    llm: LLM,
    run_dir: Path,
    *,
    run_judge: bool = True,
    label: str = "",
    workers: int | None = None,
) -> dict[str, ScenarioResult]:
    """Run scenarios, in parallel when configured.

    Scenarios are independent — each builds its own World and its own simulators — so
    they parallelise cleanly. The shared LLM object is only used for config lookups and
    usage counters, and those counters are guarded by a lock.
    """
    prefix = f"[{label}] " if label else ""
    if workers is None:
        workers = llm.limit("parallel_scenarios", 1)
    workers = max(1, min(int(workers), len(scenarios) or 1))

    results: dict[str, ScenarioResult] = {}
    lock = threading.Lock()

    def record(spec: dict[str, Any], result: ScenarioResult) -> None:
        with lock:
            results[spec["id"]] = result
            save_result(result, run_dir)
            mark = "PASS" if result.passed else "FAIL"
            print(f"  {prefix}{mark} {spec['id']}", flush=True)
            if not result.passed:
                for failure in result.failures[:4]:
                    print(f"        - {failure['name']}: {failure['detail'][:140]}")

    if workers == 1:
        for spec in scenarios:
            print(f"  {prefix}running {spec['id']} ...", flush=True)
            record(spec, run_scenario(spec, llm, run_judge=run_judge))
        return results

    print(f"  {prefix}running {len(scenarios)} scenarios, {workers} at a time ...", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_scenario, spec, llm, run_judge=run_judge): spec
            for spec in scenarios
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                record(spec, future.result())
            except Exception as exc:  # noqa: BLE001 - one bad scenario must not kill the suite
                with lock:
                    print(f"  {prefix}ERROR {spec['id']}: {type(exc).__name__}: {exc}", flush=True)
    return results


def heal(
    scenarios: list[dict[str, Any]],
    *,
    suite: str,
    max_repair_rounds: int = 5,
    run_judge: bool = True,
    regression: bool = True,
    workers: int | None = None,
) -> LoopReport:
    llm = LLM()
    run_dir = new_run_dir(f"loop-{suite}")
    by_id = {s["id"]: s for s in scenarios}

    report = LoopReport(
        suite=suite,
        started_at=datetime.now().isoformat(),
        scenarios=list(by_id),
    )

    print(f"\n=== Round 0: baseline ({len(scenarios)} scenarios) ===")
    results = run_suite(scenarios, llm, run_dir / "round-0", run_judge=run_judge, workers=workers)
    report.initial_failures = [sid for sid, r in results.items() if not r.passed]
    print(f"\nBaseline: {len(scenarios) - len(report.initial_failures)}/{len(scenarios)} passed")

    # ------------------------------------------------------------------
    for scenario_id in list(report.initial_failures):
        spec = by_id[scenario_id]
        attempts: list[str] = []
        print(f"\n=== Repairing {scenario_id} ===")

        for round_number in range(1, max_repair_rounds + 1):
            current = results[scenario_id]
            if current.passed:
                break

            print(f"  [round {round_number}] doctor analysing ...")
            patch = doctor.propose(llm, current, spec, attempts)
            if patch is None:
                report.repairs.append(
                    RepairRecord(scenario_id, round_number, "-", "doctor produced no usable patch", "rejected")
                )
                break

            print(f"  [round {round_number}] editing agents/{patch.file}: {patch.reason[:120]}")
            doctor.apply(patch)

            retry = run_scenario(spec, llm, run_judge=run_judge)
            results[scenario_id] = retry
            save_result(retry, run_dir / f"repair-{scenario_id}-r{round_number}")

            if not retry.passed:
                print("  FAIL - still failing after the edit; reverting")
                doctor.revert(patch)
                results[scenario_id] = current
                attempts.append(
                    f"edited {patch.file} ({patch.reason[:80]}) -> still failing: "
                    + "; ".join(f["name"] for f in retry.failures[:3])
                )
                report.repairs.append(
                    RepairRecord(
                        scenario_id,
                        round_number,
                        patch.file,
                        patch.reason,
                        "reverted_scenario_still_failing",
                        history_path=str(patch.history_path or ""),
                    )
                )
                continue

            # --- Scenario passes; now regress ---------------------------
            if not regression:
                print("  PASS - scenario fixed (regression skipped)")
                report.repairs.append(
                    RepairRecord(scenario_id, round_number, patch.file, patch.reason, "fixed",
                                 history_path=str(patch.history_path or ""))
                )
                break

            others = [s for s in scenarios if s["id"] != scenario_id]
            print(f"  PASS - scenario fixed; running regression over {len(others)} scenarios ...")
            regression_results = run_suite(
                others,
                llm,
                run_dir / f"regression-{scenario_id}-r{round_number}",
                run_judge=run_judge,
                label="regression",
                workers=workers,
            )
            broke = [
                sid
                for sid, r in regression_results.items()
                if not r.passed and results.get(sid) and results[sid].passed
            ]

            if broke:
                print(f"  FAIL - regression broken: {broke}; reverting")
                doctor.revert(patch)
                results[scenario_id] = current
                attempts.append(
                    f"edited {patch.file} ({patch.reason[:80]}) -> fixed this scenario but broke {broke}"
                )
                report.repairs.append(
                    RepairRecord(
                        scenario_id,
                        round_number,
                        patch.file,
                        patch.reason,
                        "reverted_regression",
                        regression_broke=broke,
                        history_path=str(patch.history_path or ""),
                    )
                )
                continue

            print("  PASS - regression clean; keeping the change")
            results.update(regression_results)
            report.repairs.append(
                RepairRecord(scenario_id, round_number, patch.file, patch.reason, "fixed",
                             history_path=str(patch.history_path or ""))
            )
            break

    # ------------------------------------------------------------------
    report.final_failures = [sid for sid, r in results.items() if not r.passed]
    report.usage = llm.usage.as_dict()
    report.results = {sid: r.as_dict() for sid, r in results.items()}

    write_report(report, run_dir)
    print_summary(report, run_dir)
    return report


def write_report(report: LoopReport, run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    total = len(report.scenarios)
    passed = total - len(report.final_failures)

    lines = [
        f"# Self-healing run report - {report.suite}",
        "",
        f"- Started: {report.started_at}",
        f"- Scenarios: {total}",
        f"- Baseline passing: {total - len(report.initial_failures)}/{total}",
        f"- **Final passing: {passed}/{total}**",
        "",
        "### Token usage",
        "",
        f"- Calls: {report.usage.get('calls', 0)}",
        f"- Input / output: {report.usage.get('prompt_tokens', 0):,} / "
        f"{report.usage.get('completion_tokens', 0):,}",
        f"- Cache hits: {report.usage.get('cache_hit_tokens', 0):,} tokens "
        f"({report.usage.get('cache_hit_rate', 0):.1%} hit rate, "
        f"{report.usage.get('cache_miss_tokens', 0):,} missed)",
        f"- Calls by role: {report.usage.get('by_role', {})}",
        "",
        "## Scenario results",
        "",
        "| Scenario | Baseline | Final | Agents |",
        "|---|---|---|---|",
    ]
    for scenario_id in report.scenarios:
        base = "FAIL" if scenario_id in report.initial_failures else "PASS"
        final = "FAIL" if scenario_id in report.final_failures else "PASS"
        agents = " -> ".join(report.results.get(scenario_id, {}).get("agents_involved", []))
        lines.append(f"| {scenario_id} | {base} | {final} | {agents} |")

    lines += ["", "## Prompt changes", ""]
    if not report.repairs:
        lines.append("(no prompt changes were made)")
    for repair in report.repairs:
        icon = {"fixed": "KEPT", "rejected": "NO PATCH"}.get(repair.outcome, "REVERTED")
        lines += [
            f"### {repair.scenario_id} round {repair.round_number} - {icon}",
            "",
            f"- File: `agents/{repair.file}`",
            f"- Reason: {repair.reason}",
        ]
        if repair.regression_broke:
            lines.append(f"- Scenarios broken in regression: {repair.regression_broke}")
        if repair.history_path:
            lines.append(f"- Snapshot: `{repair.history_path}`")
        lines.append("")

    if report.final_failures:
        lines += ["## Still failing (needs a human)", ""]
        for scenario_id in report.final_failures:
            result = report.results.get(scenario_id, {})
            lines.append(f"### {scenario_id}")
            lines.append("")
            for failure in result.get("failures", []):
                lines.append(f"- **{failure['name']}**: {failure['detail']}")
            lines.append("")

    path = run_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "suite": report.suite,
                "started_at": report.started_at,
                "scenarios": report.scenarios,
                "initial_failures": report.initial_failures,
                "final_failures": report.final_failures,
                "repairs": [r.__dict__ for r in report.repairs],
                "usage": report.usage,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def print_summary(report: LoopReport, run_dir: Path) -> None:
    total = len(report.scenarios)
    passed = total - len(report.final_failures)
    print("\n" + "=" * 60)
    print(f"Final: {passed}/{total} passed")
    if report.final_failures:
        print(f"Still failing: {report.final_failures}")
    kept = [r for r in report.repairs if r.outcome == "fixed"]
    print(f"Prompt changes kept: {len(kept)}; reverted: {len(report.repairs) - len(kept)}")
    usage = report.usage
    print(
        f"tokens: input {usage.get('prompt_tokens', 0):,} / output "
        f"{usage.get('completion_tokens', 0):,}; "
        f"cache hits {usage.get('cache_hit_tokens', 0):,} "
        f"({usage.get('cache_hit_rate', 0):.1%})"
    )
    print(f"Report: {run_dir / 'report.md'}")
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a scenario suite and auto-repair agent prompts")
    parser.add_argument("--suite", default="intake", help="Suite directory name, or all")
    parser.add_argument("--scenario", action="append", help="Run only these scenario ids; repeatable")
    parser.add_argument("--max-repair-rounds", type=int, default=5)
    parser.add_argument("--no-judge", action="store_true", help="Skip the LLM judge")
    parser.add_argument("--no-regression", action="store_true", help="Do not run full regression after a patch passes")
    parser.add_argument("--baseline-only", action="store_true", help="Baseline only, no self-healing")
    parser.add_argument("--workers", type=int, default=None,
                        help="Scenarios to run at once (default: parallel_scenarios in llm.yaml)")
    args = parser.parse_args(argv)

    scenarios = (
        scenario_mod.load_all() if args.suite == "all" else scenario_mod.load_suite(args.suite)
    )
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [s for s in scenarios if s["id"] in wanted]
        if not scenarios:
            print(f"No scenarios matched: {sorted(wanted)}")
            return 2

    report = heal(
        scenarios,
        suite=args.suite,
        workers=args.workers,
        max_repair_rounds=0 if args.baseline_only else args.max_repair_rounds,
        run_judge=not args.no_judge,
        regression=not args.no_regression,
    )
    return 0 if not report.final_failures else 1


if __name__ == "__main__":
    sys.exit(main())

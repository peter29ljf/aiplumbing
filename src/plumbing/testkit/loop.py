"""The self-healing loop: run the suite, let doctor edit prompts, re-run, regress, revert.

    PYTHONPATH=src python3 -m plumbing.testkit.loop --suite intake --max-repair-rounds 5

Regression is the critical step. Doctor will happily hard-code a rule to fix scenario A
and break scenario B doing it. Every patch re-runs the scenarios that already passed, and
anything that breaks them is reverted.

Every scenario runs `--repeat` times and gets one of three verdicts. The customer simulator
samples at high temperature, so a single run is not evidence: the same code run twice has
produced five failures and eight, with only four in common. A scenario that passes once and
fails once is **flaky**, and flaky scenarios are never handed to doctor — it would rewrite a
prompt that was only unlucky, and the revert-on-regression machinery cannot tell the
difference either.
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
class ScenarioVerdict:
    """What several runs of one scenario add up to."""

    scenario_id: str
    runs: list[ScenarioResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        passed = [r.passed for r in self.runs]
        if all(passed):
            return "pass"
        if not any(passed):
            return "fail"
        return "flaky"

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    @property
    def actionable(self) -> bool:
        """Only a scenario that fails every time is worth acting on."""
        return self.verdict == "fail"

    @property
    def representative(self) -> ScenarioResult:
        """A failing run if there is one — that is what doctor needs to read."""
        for run in self.runs:
            if not run.passed:
                return run
        return self.runs[0]

    @property
    def summary(self) -> str:
        wins = sum(1 for r in self.runs if r.passed)
        return f"{wins}/{len(self.runs)} passed"


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
    flaky: list[str] = field(default_factory=list)
    repeat: int = 1
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
    repeat: int = 1,
    confirm_failures: bool = True,
) -> dict[str, ScenarioVerdict]:
    """Run each scenario `repeat` times, in parallel, and return one verdict each.

    Scenarios are independent — each builds its own World and its own simulators — so they
    parallelise cleanly, and so do repeats of the same scenario. The shared LLM object is
    only used for config lookups and usage counters, and those are guarded by a lock.

    Anything that fails every run is then run `repeat` more times before the verdict
    stands, because that verdict is what sends doctor after a prompt.
    """
    prefix = f"[{label}] " if label else ""
    if workers is None:
        workers = llm.limit("parallel_scenarios", 1)
    jobs = [(spec, attempt) for spec in scenarios for attempt in range(max(1, repeat))]
    workers = max(1, min(int(workers), len(jobs) or 1))

    verdicts: dict[str, ScenarioVerdict] = {
        spec["id"]: ScenarioVerdict(spec["id"]) for spec in scenarios
    }
    lock = threading.Lock()

    done = {"n": 0}

    def record(spec: dict[str, Any], attempt: int, result: ScenarioResult) -> None:
        with lock:
            verdicts[spec["id"]].runs.append(result)
            save_result(result, run_dir / (f"attempt-{attempt + 1}" if repeat > 1 else ""))
            # Report each run as it lands. Verdicts only make sense once every repeat of a
            # scenario is in, but a suite that prints nothing for twenty minutes is
            # indistinguishable from one that has hung.
            done["n"] += 1
            tag = f"{spec['id']} #{attempt + 1}" if repeat > 1 else spec["id"]
            print(f"  {prefix}[{done['n']}/{len(jobs)}] "
                  f"{'ok  ' if result.passed else 'FAIL'} {tag}", flush=True)

    note = f", {repeat}x each" if repeat > 1 else ""
    print(f"  {prefix}running {len(scenarios)} scenarios{note}, {workers} at a time ...",
          flush=True)

    if workers == 1:
        for spec, attempt in jobs:
            record(spec, attempt, run_scenario(spec, llm, run_judge=run_judge))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(run_scenario, spec, llm, run_judge=run_judge): (spec, attempt)
                for spec, attempt in jobs
            }
            for future in as_completed(futures):
                spec, attempt = futures[future]
                try:
                    record(spec, attempt, future.result())
                except Exception as exc:  # noqa: BLE001 - one bad run must not kill the suite
                    with lock:
                        print(f"  {prefix}ERROR {spec['id']}: {type(exc).__name__}: {exc}",
                              flush=True)

    # A scenario that fails every run is about to be handed to doctor, so it is worth
    # being sure. With repeat=2 a genuinely 50/50 scenario comes out "fail" a quarter of
    # the time — enough to send doctor after a prompt that was only unlucky. Re-running
    # just the failures costs little, because there are few of them.
    if repeat > 1 and confirm_failures:
        suspects = [s for s in scenarios if verdicts[s["id"]].verdict == "fail"]
        if suspects:
            print(f"  {prefix}confirming {len(suspects)} failure(s) with {repeat} more "
                  f"run(s) each ...", flush=True)
            extra = [(spec, i) for spec in suspects for i in range(repeat)]
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(extra)))) as pool:
                futures = {
                    pool.submit(run_scenario, spec, llm, run_judge=run_judge): spec
                    for spec, _ in extra
                }
                for future in as_completed(futures):
                    spec = futures[future]
                    try:
                        with lock:
                            verdicts[spec["id"]].runs.append(future.result())
                    except Exception:  # noqa: BLE001
                        pass

    print(f"  {prefix}--- verdicts ---", flush=True)
    for scenario_id in [s["id"] for s in scenarios]:
        entry = verdicts[scenario_id]
        if not entry.runs:
            continue
        mark = {"pass": "PASS", "fail": "FAIL", "flaky": "FLAKY"}[entry.verdict]
        suffix = f"  ({entry.summary})" if repeat > 1 and entry.verdict != "pass" else ""
        print(f"  {prefix}{mark} {scenario_id}{suffix}", flush=True)
        if entry.verdict != "pass":
            for failure in entry.representative.failures[:4]:
                print(f"        - {failure['name']}: {failure['detail'][:140]}")

    return verdicts


def heal(
    scenarios: list[dict[str, Any]],
    *,
    suite: str,
    max_repair_rounds: int = 5,
    run_judge: bool = True,
    regression: bool = True,
    workers: int | None = None,
    repeat: int = 2,
) -> LoopReport:
    llm = LLM()
    run_dir = new_run_dir(f"loop-{suite}")
    by_id = {s["id"]: s for s in scenarios}

    report = LoopReport(
        suite=suite,
        started_at=datetime.now().isoformat(),
        scenarios=list(by_id),
        repeat=repeat,
    )

    print(f"\n=== Round 0: baseline ({len(scenarios)} scenarios) ===")
    results = run_suite(scenarios, llm, run_dir / "round-0", run_judge=run_judge,
                        workers=workers, repeat=repeat)
    report.initial_failures = [sid for sid, v in results.items() if v.verdict == "fail"]
    report.flaky = [sid for sid, v in results.items() if v.verdict == "flaky"]
    passing = sum(1 for v in results.values() if v.passed)
    print(f"\nBaseline: {passing}/{len(scenarios)} passed, "
          f"{len(report.initial_failures)} failed, {len(report.flaky)} flaky")
    if report.flaky:
        print("  Flaky scenarios are NOT sent to doctor — a prompt that was only unlucky")
        print(f"  must not be rewritten: {report.flaky}")

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
            patch = doctor.propose(llm, current.representative, spec, attempts)
            if patch is None:
                report.repairs.append(
                    RepairRecord(scenario_id, round_number, "-", "doctor produced no usable patch", "rejected")
                )
                break

            print(f"  [round {round_number}] editing agents/{patch.file}: {patch.reason[:120]}")
            doctor.apply(patch)

            retry = run_suite([spec], llm, run_dir / f"repair-{scenario_id}-r{round_number}",
                              run_judge=run_judge, workers=1, repeat=repeat)[scenario_id]
            results[scenario_id] = retry

            if not retry.passed:
                print("  FAIL - still failing after the edit; reverting")
                doctor.revert(patch)
                results[scenario_id] = current
                attempts.append(
                    f"edited {patch.file} ({patch.reason[:80]}) -> still failing: "
                    + "; ".join(f["name"] for f in retry.representative.failures[:3])
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
                repeat=repeat,
            )
            # Only a scenario that went from reliably passing to reliably failing counts as
            # broken. A newly flaky one is not evidence the patch did it.
            broke = [
                sid
                for sid, v in regression_results.items()
                if v.verdict == "fail" and results.get(sid) and results[sid].passed
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
    report.final_failures = [sid for sid, v in results.items() if v.verdict == "fail"]
    report.flaky = [sid for sid, v in results.items() if v.verdict == "flaky"]
    report.usage = llm.usage.as_dict()
    report.results = {
        sid: {**v.representative.as_dict(), "verdict": v.verdict, "runs": v.summary}
        for sid, v in results.items()
    }

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
        f"- **Final: {passed} passing, {len(report.final_failures)} failing, "
        f"{len(report.flaky)} flaky** (of {total})",
        f"- Each scenario run {report.repeat}x; a scenario that passes some runs and fails "
        f"others is flaky and was **not** sent to doctor",
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
        "| Scenario | Baseline | Final | Runs | Agents |",
        "|---|---|---|---|---|",
    ]

    def verdict_of(scenario_id: str, failures: list[str]) -> str:
        if scenario_id in failures:
            return "FAIL"
        if scenario_id in report.flaky:
            return "FLAKY"
        return "PASS"

    for scenario_id in report.scenarios:
        entry = report.results.get(scenario_id, {})
        agents = " -> ".join(entry.get("agents_involved", []))
        lines.append(
            f"| {scenario_id} | {verdict_of(scenario_id, report.initial_failures)} "
            f"| {verdict_of(scenario_id, report.final_failures)} "
            f"| {entry.get('runs', '')} | {agents} |"
        )

    if report.flaky:
        lines += [
            "",
            "## Flaky — not acted on",
            "",
            "These passed some runs and failed others. The customer simulator samples at high",
            "temperature, so that is not evidence of a broken prompt. Sending them to doctor",
            "would rewrite prompts that were only unlucky.",
            "",
        ]
        for scenario_id in report.flaky:
            entry = report.results.get(scenario_id, {})
            lines.append(f"- **{scenario_id}** ({entry.get('runs', '')})")
            for failure in entry.get("failures", [])[:3]:
                lines.append(f"  - {failure['name']}: {failure['detail'][:120]}")

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
        lines += ["", "## Still failing every run (needs a human)", ""]
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
                "repeat": report.repeat,
                "initial_failures": report.initial_failures,
                "final_failures": report.final_failures,
                "flaky": report.flaky,
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
    print(f"Final: {passed} passing, {len(report.final_failures)} failing, "
          f"{len(report.flaky)} flaky (of {total}, {report.repeat}x each)")
    if report.final_failures:
        print(f"Failing every run: {report.final_failures}")
    if report.flaky:
        print(f"Flaky, not acted on:  {report.flaky}")
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
    parser.add_argument("--repeat", type=int, default=2,
                        help="Times to run each scenario before judging it (default 2). "
                             "A scenario that passes some runs and fails others is flaky "
                             "and is not sent to doctor.")
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
        repeat=max(1, args.repeat),
        max_repair_rounds=0 if args.baseline_only else args.max_repair_rounds,
        run_judge=not args.no_judge,
        regression=not args.no_regression,
    )
    return 0 if not report.final_failures else 1


if __name__ == "__main__":
    sys.exit(main())

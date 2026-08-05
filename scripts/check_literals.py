#!/usr/bin/env python3
"""Deterministic prompt checks. No model call, no cost, runs in milliseconds.

The LLM scan in check_consistency.py reads for meaning and costs a call. Three of the
things it kept finding do not need meaning at all — they are pattern matching, and
pattern matching is free, instant and exactly reproducible. Run this first; run the
model only on what is left.

  1. money literals   — a price written into a prompt instead of read from rules.*
  2. dangling tools   — a prompt telling the agent to call something that does not exist
  3. retired terms    — wording from before a rename that some file still carries

Check 2 exists because of a mistake made while fixing check 1: four prices were moved
into business_rules.yaml and the prompts were pointed at `rules.get_job_sizing` to read
them back — before that tool returned them. Swapping a stale number for a lookup that
returns nothing is worse than the number. This catches that in milliseconds.

    python3 scripts/check_literals.py
    python3 scripts/check_literals.py --quiet   # exit code only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.paths import AGENTS_DIR, SHARED_DIR  # noqa: E402


# Money written out in prose. Deliberately narrow: a bare number is usually a step
# number or a count of questions, and flagging those buries the real findings.
MONEY = re.compile(r"\b(?:CAD|USD)\s*[\d,]+|\$\s*[\d,]+|\b[\d,]+\s*(?:dollars|CAD)\b", re.I)

# Durations and periods stated as words. These go stale the same way prices do.
PERIODS = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twelve|24|48|72)[\s-]"
    r"(?:year|month|week|working\s+day|day|hour|minute|round)s?\b",
    re.I,
)

# Wording retired by a rename or a move. Add to this whenever something is renamed —
# the cost of a stale entry is nil, the cost of a missing one is an agent telling a
# customer the old company name, which has happened.
RETIRED = {
    "Maple Plumbing": "company was renamed to Fangxin Plumbing Ltd",
    "Toronto": "service region moved to Metro Vancouver",
    "Ontario": "service region moved to Metro Vancouver",
    "EST": "timezone is America/Vancouver",
    "supervisor decides the warranty": "warranty verdicts go to the on-duty technician",
}

def _retired_pattern(term: str) -> re.Pattern[str]:
    """Word-bounded, and case-sensitive when the term is an acronym.

    Matching retired terms as plain substrings finds "EST" inside "request", "suggest"
    and "best" — thirty-two false positives on the first run, which is how a checker
    stops being read.
    """
    flags = 0 if term.isupper() else re.I
    return re.compile(rf"\b{re.escape(term)}\b", flags)


RETIRED_PATTERNS = {term: _retired_pattern(term) for term in RETIRED}

# A dotted tool name as it appears in a prompt: `crm.lookup_by_phone`
TOOL_REF = re.compile(r"`([a-z_]+\.[a-z_]+)`")

# A bare agent name in backticks: `small_job`. Distinguished from a tool by having no dot.
AGENT_REF = re.compile(r"`([a-z_]+)`")

# Phrases that read like tool calls but are not, and prose that legitimately carries a
# figure. Anything listed here is skipped rather than reported.
ALLOW_SUBSTRINGS = (
    "one or two things",       # how many questions to ask at once
    "one or two digit",        # unit-number heuristic
    "one round, not a habit",  # asking for detail
    "two things survive",      # the apartment filter carve-out
    "two reasons",             # prose
    "one sentence",
    "one page",
    "a turn of its own",
    "six months later",        # prose about why email beats a file store
)


def prompt_files() -> list[Path]:
    return sorted(SHARED_DIR.glob("*.md")) + sorted(AGENTS_DIR.glob("*.md"))


def known_tools() -> set[str]:
    from plumbing.tools import registry  # noqa: PLC0415

    return {spec["name"] for spec in registry.catalog() if spec.get("status") != "planned"}


def agent_names() -> tuple[set[str], set[str]]:
    """(every agent in agents.yaml, the ones this deployment runs)."""
    from plumbing import config  # noqa: PLC0415

    return set(config.agents_config()["agents"]), set(config.enabled_agents())


def live_prompt_files() -> set[str]:
    """Just the files an enabled agent actually assembles.

    A switched-off agent's own prompt naturally names itself and its neighbours, and none
    of it is loaded in production. Flagging those buries the handful that matter — the
    first run of this check reported ten findings, of which four were an unused file
    talking about itself.
    """
    from plumbing import config  # noqa: PLC0415

    cfg = config.agents_config()["agents"]
    live: set[str] = set()
    for name in config.enabled_agents():
        spec = cfg.get(name) or {}
        live.add(f"agents/{spec.get('prompt', name + '.md')}")
        for fragment in spec.get("shared") or []:
            live.add(f"agents/_shared/{fragment}.md")
    return live


def rel(path: Path) -> str:
    return str(path).split("/plumbing/", 1)[-1]


def scan() -> list[tuple[str, str, int, str, str]]:
    """(severity, kind, line, location, detail)"""
    findings: list[tuple[str, str, int, str, str]] = []
    tools = known_tools()
    all_agents, live_agents = agent_names()
    switched_off = all_agents - live_agents
    in_production = live_prompt_files()

    for path in prompt_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(a in line.lower() for a in ALLOW_SUBSTRINGS):
                continue
            where = f"{rel(path)}:{n}"

            for m in MONEY.finditer(line):
                findings.append(
                    ("HIGH", "money-literal", n, where,
                     f"{m.group(0)!r} — a price in a prompt goes stale silently; read it from rules.*")
                )
            for m in PERIODS.finditer(line):
                findings.append(
                    ("MEDIUM", "period-literal", n, where,
                     f"{m.group(0)!r} — state the source, not the figure")
                )
            for term, why in RETIRED.items():
                if RETIRED_PATTERNS[term].search(line):
                    findings.append(("HIGH", "retired-term", n, where, f"{term!r} — {why}"))
            for m in TOOL_REF.finditer(line):
                name = m.group(1)
                if name not in tools:
                    findings.append(
                        ("HIGH", "dangling-tool", n, where,
                         f"`{name}` is not a live tool — the agent is being sent somewhere that does not answer")
                    )
            # A prompt that routes to an agent this deployment does not run sends the
            # customer into silence. The tool refuses the transfer, but by then the agent
            # has usually already told them it is arranging something.
            for m in AGENT_REF.finditer(line):
                name = m.group(1)
                if name in switched_off and rel(path) in in_production:
                    findings.append(
                        ("HIGH", "disabled-agent", n, where,
                         f"`{name}` is not enabled in config/live.yaml — routing here reaches nobody")
                    )

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    findings.sort(key=lambda f: (order[f[0]], f[3]))
    return findings


# Lines that must be caught, and lines that must not. A checker reporting nothing looks
# exactly like a checker that has stopped working — the first run of this one produced
# thirty-two false positives from "EST" inside "request", and the fix could just as
# easily have silenced it entirely. These are the samples that tell the two apart.
SELF_TEST_MUST_CATCH = [
    ("money", "The call-out fee is **CAD 100** for the technician to attend."),
    ("money", "a $250 deposit is required"),
    ("period", "come back with a quote in about two working days"),
    ("period", "drain cleaning carries no one-year warranty"),
    ("retired", "You are an agent for Maple Plumbing in Toronto."),
    ("dangling", "Call `rules.get_unicorn_policy` before quoting."),
    ("disabled", "If they mention warranty, hand off to `warranty` immediately."),
]
SELF_TEST_MUST_IGNORE = [
    "Ask one or two things at a time, not three questions at once.",
    "Call `crm.lookup_by_phone` once you have the number.",
    "Getting the last point in now costs one sentence.",
    "a file store full of anonymous photos helps nobody six months later",
    "Do not suggest they describe the property differently.",   # 'suggest' contains EST
    "the best time to ask is before you book",                  # 'best' contains EST
]


def self_test() -> int:
    tools = known_tools()

    def hits(line: str) -> list[str]:
        if any(a in line.lower() for a in ALLOW_SUBSTRINGS):
            return []
        found = []
        if MONEY.search(line):
            found.append("money")
        if PERIODS.search(line):
            found.append("period")
        if any(pat.search(line) for pat in RETIRED_PATTERNS.values()):
            found.append("retired")
        if any(m.group(1) not in tools for m in TOOL_REF.finditer(line)):
            found.append("dangling")
        _, live = agent_names()
        off = set(agent_names()[0]) - live
        if any(m.group(1) in off for m in AGENT_REF.finditer(line)):
            found.append("disabled")
        return found

    failures = 0
    for kind, line in SELF_TEST_MUST_CATCH:
        got = hits(line)
        ok = kind in got
        failures += not ok
        print(f"  {'ok  ' if ok else 'MISS'} expect {kind:9} {line[:58]!r}")
    for line in SELF_TEST_MUST_IGNORE:
        got = hits(line)
        failures += bool(got)
        print(f"  {'ok  ' if not got else 'FALSE POSITIVE ' + str(got)} clean     {line[:58]!r}")

    print(f"\nself-test: {'PASS' if not failures else f'FAIL ({failures})'}")
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the detectors still fire, and still ignore what they should")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    findings = scan()
    if not args.quiet:
        files = len(prompt_files())
        print(f"Checked {files} prompt files — no model call.\n")
        for severity, kind, _line, where, detail in findings:
            print(f"{severity:6} {kind:15} {where}")
            print(f"       {detail}")
        highs = sum(1 for f in findings if f[0] == "HIGH")
        print(f"\n{'=' * 60}\n{len(findings)} finding(s), {highs} high")

    return 1 if any(f[0] == "HIGH" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())

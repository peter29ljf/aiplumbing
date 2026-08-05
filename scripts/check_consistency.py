#!/usr/bin/env python3
"""Read every prompt and config, and report where they contradict each other.

The cheapest check we have: no scenarios, no simulators, no agent runs — one model
call over the whole corpus. It exists because a contradiction that costs money
(three different refund cut-offs, one of them a retired rule) survived 156 end-to-end
runs without ever being caught. Reading found it in minutes.

`config/business_rules.yaml` is the authority. Anything a prompt says that disagrees
with it is a finding, and the prompt is what is wrong.

**Every finding must quote the text it is about, and the quote is verified against the
file before the finding is shown.** A model asked to cite line numbers will invent them.
Findings whose quotes cannot be located are still printed, but marked UNVERIFIED and
excluded from the exit code — otherwise this tool becomes one more thing that has to be
double-checked by hand, which is exactly what it is meant to replace.

    python3 scripts/check_consistency.py
    python3 scripts/check_consistency.py --json report.json
    python3 scripts/check_consistency.py --expect "refund"   # self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.llm import LLM, _extract_json  # noqa: E402
from plumbing.paths import AGENTS_DIR, CONFIG_DIR, SHARED_DIR  # noqa: E402


# Configs an agent's behaviour is supposed to follow. Ordered so the authority comes
# first in the prompt — the model should read the rules before the prose about them.
CONFIG_FILES = ["business_rules.yaml", "ticket_states.yaml"]

SYSTEM = """You audit the prompt files of a customer-service agent system for
**internal contradictions**. You are reading the whole corpus at once, which no single
agent ever does — that is the point of this pass.

`config/business_rules.yaml` is the single source of truth for prices, hours, holidays,
warranty terms, refund rules and escalation triggers. `config/ticket_states.yaml` is the
authority on which ticket transitions are legal. When a prompt disagrees with either,
**the prompt is what is wrong**.

Report only these kinds of finding:

- `contradiction` — two files instruct the agent to do incompatible things, or a prompt
  contradicts the authoritative config. Include a stale rule that was replaced but left
  behind somewhere.
- `unsourced_value` — a prompt states a price, duration, period or threshold as a literal
  instead of telling the agent to look it up, AND that literal could go out of date.
- `illegal_transition` — a prompt instructs a ticket status change that ticket_states.yaml
  does not allow from the state the prompt has the agent in.
- `dangling_reference` — a prompt points at a section, tool or rule that does not exist.

**Read every cut-off, deadline and threshold twice.** The costly disagreements in this
corpus do not look like disagreements: two rules both say "no X after Y" and read as
consistent, while naming *different moments* for Y. Wherever a rule turns on a boundary —
a cut-off, a deadline, a limit, a point of no return, an eligibility threshold — find every
other place that boundary is stated and check they name the **same** event or number. Two
events that usually happen close together are still two events, and the window between them
is where money moves wrongly.

Do NOT report: stylistic differences, repetition, wordiness, missing features, or
anything you merely think could be phrased better. Deliberate repetition of the same rule
across several agents is a defence in depth, not a finding — unless the repeated copies
**say different things**, which is.

For every finding, quote a short span of the actual text you are talking about, copied
character for character from the file. The quote is how the finding gets verified; a
paraphrase makes it unverifiable and it will be discarded.

Output a single JSON object:

{"findings": [
  {"kind": "contradiction",
   "severity": "high|medium|low",
   "summary": "one line, what disagrees with what",
   "why_it_matters": "the concrete consequence when this fires",
   "locations": [
     {"file": "agents/_shared/core_rules.md", "quote": "exact text from that file"},
     {"file": "config/business_rules.yaml", "quote": "exact text from that file"}
   ],
   "authority": "which file should win, and what it says"}
]}

severity `high` means it costs money, breaks a promise to a customer, or makes the agent
behave differently run to run. Empty findings list is a valid answer."""


def collect() -> tuple[str, dict[str, str]]:
    """Return the corpus as one prompt-ready string, plus {relpath: text} for verifying."""
    files: dict[str, str] = {}
    for name in CONFIG_FILES:
        path = CONFIG_DIR / name
        if path.exists():
            files[f"config/{name}"] = path.read_text()
    for path in sorted(SHARED_DIR.glob("*.md")):
        files[f"agents/_shared/{path.name}"] = path.read_text()
    for path in sorted(AGENTS_DIR.glob("*.md")):
        files[f"agents/{path.name}"] = path.read_text()

    blocks = []
    for rel, text in files.items():
        numbered = "\n".join(f"{i:4} | {line}" for i, line in enumerate(text.splitlines(), 1))
        blocks.append(f"===== {rel} =====\n{numbered}")
    return "\n\n".join(blocks), files


def locate(files: dict[str, str], rel: str, quote: str) -> int | None:
    """Line number where `quote` starts in `rel`, or None if it does not occur.

    Whitespace is normalised because prompts are hard-wrapped and a model quoting across
    a line break will not reproduce the newline. The whole file is flattened once and
    each normalised character is mapped back to its source line, so a quote may span any
    number of lines — an earlier version slid a window of up to four lines over the file
    and reported real findings as unverifiable whenever the quote was longer than that.
    Anything shorter than a few characters is treated as unverifiable rather than
    trivially matching.
    """
    text = files.get(rel)
    if text is None or len(quote.strip()) < 8:
        return None

    flat: list[str] = []
    line_of: list[int] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for token in line.split():
            if flat:
                flat.append(" ")
                line_of.append(lineno)
            flat.append(token)
            line_of.append(lineno)

    haystack = "".join(flat)
    # Character offset of each piece, so a match offset can be mapped back to a line.
    offsets: list[int] = []
    pos = 0
    for piece in flat:
        offsets.append(pos)
        pos += len(piece)

    idx = haystack.find(" ".join(quote.split()))
    if idx < 0:
        return None
    import bisect

    return line_of[min(bisect.bisect_right(offsets, idx) - 1, len(line_of) - 1)]


def verify(findings: list[dict[str, Any]], files: dict[str, str]) -> list[dict[str, Any]]:
    """Attach real line numbers, and mark findings whose quotes could not be found."""
    for f in findings:
        checked = []
        for loc in f.get("locations") or []:
            rel, quote = loc.get("file", ""), loc.get("quote", "")
            line = locate(files, rel, quote)
            checked.append({**loc, "line": line, "verified": line is not None})
        f["locations"] = checked
        f["verified"] = bool(checked) and all(c["verified"] for c in checked)
    return findings


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def report(findings: list[dict[str, Any]]) -> None:
    findings = sorted(
        findings,
        key=lambda f: (not f.get("verified"), SEVERITY_ORDER.get(f.get("severity", "low"), 3)),
    )
    verified = [f for f in findings if f.get("verified")]
    unverified = [f for f in findings if not f.get("verified")]

    for f in findings:
        mark = "" if f.get("verified") else "  [UNVERIFIED — quote not found in file]"
        print(f"\n{f.get('severity', '?').upper():6} {f.get('kind', '?')}{mark}")
        print(f"  {f.get('summary', '')}")
        if f.get("why_it_matters"):
            print(f"  → {f['why_it_matters']}")
        for loc in f.get("locations") or []:
            where = f"{loc['file']}:{loc['line']}" if loc.get("line") else f"{loc['file']}:?"
            print(f"     {where}")
            print(f"       {(loc.get('quote') or '')[:120]}")
        if f.get("authority"):
            print(f"  authority: {f['authority']}")

    print(
        f"\n{'=' * 60}\n{len(verified)} verified finding(s)"
        + (f", {len(unverified)} unverified (not counted)" if unverified else "")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="also write the raw findings here")
    ap.add_argument(
        "--expect",
        metavar="TEXT",
        help="self-test: exit non-zero unless a verified finding mentions TEXT",
    )
    args = ap.parse_args()

    corpus, files = collect()
    print(f"Auditing {len(files)} files, {sum(len(t.splitlines()) for t in files.values())} lines ...")

    llm = LLM()
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": corpus},
    ]
    # A reasoning model bills its thinking against max_tokens. Run it long enough and the
    # budget is gone before it writes anything, which arrives here as an empty string and
    # then as an unhelpful "failed to return valid JSON". Say what actually happened.
    raw = llm.chat_text_json_mode("auditor", messages)
    if not raw.strip():
        print(
            "The model returned nothing. On a reasoning model this means max_tokens ran out\n"
            "during thinking — raise `roles.auditor.max_tokens` in config/llm.yaml.",
            file=sys.stderr,
        )
        return 2
    result = _extract_json(raw)
    if result is None:  # malformed rather than empty; let chat_json re-ask with the error
        result = llm.chat_json("auditor", messages)
    findings = verify(result.get("findings") or [], files)
    report(findings)

    if args.json:
        Path(args.json).write_text(json.dumps(findings, indent=2, ensure_ascii=False))
        print(f"written to {args.json}")

    verified = [f for f in findings if f.get("verified")]

    if args.expect:
        needle = args.expect.lower()
        hit = any(
            needle in json.dumps(f, ensure_ascii=False).lower() for f in verified
        )
        print(f"\nself-test: {'PASS' if hit else 'FAIL'} — looked for {args.expect!r}")
        return 0 if hit else 1

    return 1 if verified else 0


if __name__ == "__main__":
    raise SystemExit(main())

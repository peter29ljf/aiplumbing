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

from plumbing.llm import LLM, LLMError, _extract_json  # noqa: E402
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
- `dangling_reference` — a prompt points at a **section or rule** that does not exist.

**Tool names are not your job.** `scripts/check_literals.py` resolves every tool reference
against the registry in a fifth of a second, exactly, with no model involved. A list of the
tools that exist is given to you only so you never call a real one undefined — do not spend
effort checking them, and do not report one as missing.

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
from the file. The quote is how the finding gets located; punctuation and emphasis are
normalised before matching, but a paraphrase will not be found and the finding is then
discarded.

Report the disagreements you are confident about and stop. An exhaustive sweep is not
wanted here and is not affordable: this runs on every merge, and a scan that thinks for
five minutes and then runs out of budget reports **nothing at all**, which is strictly
worse than reporting the three findings it was sure of after one.

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


def tool_inventory() -> str:
    """Every registered tool name, as a reference block for the prompt.

    Without it the auditor cannot tell a real tool from an invented one, so it reports the
    real ones and misses the invented ones — it flagged `handoff.transfer` and
    `rules.check_service_eligibility` as dangling when both are registered and granted, and
    the one genuinely dangling reference it did catch it caught by luck. A checker that
    cries wolf on working code is worse than no checker: the findings stop being read.

    Not part of `files`, so no quote is ever verified against it. It is context, not corpus.
    """
    from plumbing.tools import registry  # noqa: PLC0415

    return "\n".join(f"  {name}" for name in sorted(registry.all_tools()))


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

    blocks = [
        "===== TOOLS THAT EXIST (reference, not part of the corpus) =====\n"
        "A name not on this list is a dangling reference. A name on it is not, however "
        "unfamiliar it looks — do not report one as undefined.\n"
        + tool_inventory()
    ]
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

    Three further differences are ignored, all of them measured rather than guessed. Four
    findings were discarded as unverifiable across one afternoon and every one of them was
    a true defect that then had to be fixed:

    - **Typographic punctuation.** The model writes `–` where the file has `—`, and
      straight quotes where the file has curly ones.
    - **Markdown emphasis.** It quotes `If they never reply, do not chase` from a line that
      reads `**If they never reply**, do not chase`.
    - **Comment lines inside a quoted block.** Quoting a YAML list, it leaves out the `#`
      lines between the entries, because they are not part of what it is pointing at.

    None of those make a finding wrong. Requiring them was the checker holding a reader to
    a standard of transcription, and paying for it in true positives thrown away.
    """
    text = files.get(rel)
    if text is None or len(quote.strip()) < 8:
        return None

    flat: list[str] = []
    line_of: list[int] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if _is_comment(line):
            continue
        for token in _normalise(line).split():
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

    idx = haystack.find(" ".join(_normalise(quote).split()))
    if idx < 0:
        return None
    import bisect

    return line_of[min(bisect.bisect_right(offsets, idx) - 1, len(line_of) - 1)]


# Pairs the model writes for pairs the file has. Nothing here changes what a line means.
_EQUIVALENT = str.maketrans({
    "\u2013": "-", "\u2014": "-", "\u2212": "-",     # en dash, em dash, minus
    "\u2018": "'", "\u2019": "'",                    # curly single quotes
    "\u201c": '"', "\u201d": '"',                    # curly double quotes
    "\u00a0": " ",                                   # non-breaking space
})


def _normalise(text: str) -> str:
    """Punctuation and emphasis flattened. Not lowercased — case carries meaning here,
    and `Closed` being a state name is exactly the sort of thing these findings turn on."""
    return text.translate(_EQUIVALENT).replace("**", "").replace("`", "")


def _is_comment(line: str) -> bool:
    """A `#` comment on its own line. Dropped so a quoted YAML block still matches when the
    model leaves the commentary out — it is pointing at the entries, not the prose."""
    return line.lstrip().startswith("#")


def verify(findings: list[dict[str, Any]], files: dict[str, str]) -> list[dict[str, Any]]:
    """Attach real line numbers, and mark findings whose quotes could not be found."""
    for f in findings:
        checked = []
        for loc in f.get("locations") or []:
            rel, quote = loc.get("file", ""), loc.get("quote", "")
            line = locate(files, rel, quote)
            checked.append({**loc, "line": line, "verified": line is not None})
        f["locations"] = checked
        # One located quote is enough. Requiring all of them meant a finding that points at
        # a real line in one file and paraphrases a config block in another was discarded
        # whole — and the located half was the half that mattered.
        f["verified"] = any(c["verified"] for c in checked)
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
        mark = "" if f.get("verified") else "  [UNVERIFIED — no quote could be located]"
        print(f"\n{f.get('severity', '?').upper():6} {f.get('kind', '?')}{mark}")
        print(f"  {f.get('summary', '')}")
        if f.get("why_it_matters"):
            print(f"  → {f['why_it_matters']}")
        for loc in f.get("locations") or []:
            where = (
                f"{loc['file']}:{loc['line']}" if loc.get("line")
                else f"{loc['file']}:? (not located — paraphrased or from another file)"
            )
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
    # Exit 2, not 1. A call that never completed says nothing about whether the prompts
    # agree, and letting the exception escape exits 1 — which the gate reads as "found
    # disagreements". Somebody then goes looking for a contradiction nobody reported.
    try:
        raw = llm.chat_text_json_mode("auditor", messages)
    except LLMError as exc:
        print(f"The audit could not run: {exc}", file=sys.stderr)
        return 2

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

    u = llm.usage.as_dict()
    print(
        f"\ntokens: input {u.get('prompt_tokens', 0):,} / output "
        f"{u.get('completion_tokens', 0):,}"
        + (f"; cache hits {u.get('cache_hit_tokens', 0):,} "
           f"({u.get('cache_hit_rate', 0):.0%})" if u.get("cache_hit_tokens") else "")
    )

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

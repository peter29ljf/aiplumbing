#!/usr/bin/env python3
"""Index every prompt change doctor has ever made, and say which ones are still live.

The five agent prompts were written and reviewed by a person. Doctor edits them
automatically. Each edit already leaves a full before/after record in `prompt_history/`,
and a reverted one gets a line appended saying so — but reading ten files to find out
which three survived is not an audit, so this writes the one page that answers it.

    python3 scripts/prompt_changes.py            # write prompt_history/INDEX.md
    python3 scripts/prompt_changes.py --stdout   # print instead
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing import prompt_history  # noqa: E402
from plumbing.paths import PROMPT_HISTORY_DIR  # noqa: E402


def render(records: list[dict[str, str]]) -> str:
    kept = [r for r in records if r["kept"]]
    reverted = [r for r in records if not r["kept"]]

    out = [
        "# Prompt change history",
        "",
        "Every edit doctor has made to an agent prompt. **Generated — do not edit by hand**",
        "(`python3 scripts/prompt_changes.py`).",
        "",
        f"**{len(kept)} live, {len(reverted)} reverted, {len(records)} attempted.**",
        "",
        "The agent prompts were written and reviewed by a person; the changes in the first",
        "table are the ones that are in the files now and were not. Reverted attempts are",
        "kept because a fix that failed twice is evidence about the problem, not noise.",
        "",
        "## Live — these are in the prompts right now",
        "",
        "| When | File | Because of | Why |",
        "|---|---|---|---|",
    ]
    for r in kept:
        out.append(
            f"| [{r['when']}]({r['id']}) | `{', '.join(r['files'])}` | "
            f"`{r['scenario']}` | {_short(r['reason'])} |"
        )

    out += [
        "",
        "## Reverted — attempted, did not survive its own regression",
        "",
        "| When | File | Because of | What it tried |",
        "|---|---|---|---|",
    ]
    for r in reverted:
        out.append(
            f"| [{r['when']}]({r['id']}) | `{', '.join(r['files'])}` | "
            f"`{r['scenario']}` | {_short(r['reason'])} |"
        )

    by_file: dict[str, int] = {}
    for r in kept:
        for f in r["files"]:
            by_file[f] = by_file.get(f, 0) + 1
    if by_file:
        out += ["", "## Which files doctor has changed and kept", "", "| File | Live changes |", "|---|---|"]
        for f, n in sorted(by_file.items(), key=lambda kv: -kv[1]):
            out.append(f"| `{f}` | {n} |")

    out += [
        "",
        "To see exactly what a change did, open its record: it holds the full text of the",
        "file before and after. To undo one that is live, take the *before* block from that",
        "record — `git log -- agents/` will not separate doctor's commits from anyone else's.",
        "",
    ]
    return "\n".join(out)


def _short(reason: str, limit: int = 160) -> str:
    reason = " ".join(reason.split()).replace("|", "\\|")
    return reason if len(reason) <= limit else reason[: limit - 1].rstrip() + "…"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdout", action="store_true", help="print instead of writing INDEX.md")
    args = ap.parse_args()

    recs = prompt_history.records()
    if not recs:
        print("No prompt changes recorded yet.")
        return 0

    text = render(recs)
    if args.stdout:
        print(text)
    else:
        target = PROMPT_HISTORY_DIR / "INDEX.md"
        target.write_text(text, encoding="utf-8")
        s = prompt_history.summary()
        print(f"{target}: {s['attempted']} changes, {s['live']} live, {s['reverted']} reverted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

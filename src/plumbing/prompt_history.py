"""Read the record doctor leaves behind every time it edits a prompt.

The agent prompts were written and reviewed by a person. Doctor rewrites them on its own,
so every edit gets a file in `prompt_history/` holding the reason, the triggering scenario
and the full text before and after — and `doctor.revert()` appends a line to that same file
when the change does not survive its regression.

That means the answer to "which of my prompts has been changed, and is the change still
there?" is already on disk, spread across one file per attempt. This module gathers it so
the console and the index script can both show it without either one re-implementing the
parsing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from plumbing.paths import PROMPT_HISTORY_DIR

REVERTED_MARK = "This change was reverted"

_FIELD = re.compile(r"^- (Time|Backend|Triggering scenario|Files|Reason): (.*)$", re.M)
_SECTION = re.compile(r"^## (\S+) — (before|after)\n\n```markdown\n(.*?)\n```", re.M | re.S)


def _when(name: str) -> str:
    """`20260804-165134-...` → `2026-08-04 16:51`."""
    s = name[:15]
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}"


def record(path: Path, *, with_text: bool = False) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    fields = {k: v.strip() for k, v in _FIELD.findall(text)}
    rec: dict[str, Any] = {
        "id": path.name,
        "when": _when(path.name),
        "kept": REVERTED_MARK not in text,
        "files": [f.strip() for f in fields.get("Files", "").split(",") if f.strip()],
        "scenario": fields.get("Triggering scenario", ""),
        "reason": " ".join(fields.get("Reason", "").split()),
        "backend": fields.get("Backend", ""),
    }
    if with_text:
        diffs: dict[str, dict[str, str]] = {}
        for rel, side, body in _SECTION.findall(text):
            diffs.setdefault(rel, {})[side] = body
        rec["diffs"] = diffs
    return rec


def records(*, with_text: bool = False) -> list[dict[str, Any]]:
    """Newest first. Empty when doctor has never run."""
    if not PROMPT_HISTORY_DIR.exists():
        return []
    paths = sorted(PROMPT_HISTORY_DIR.glob("2*.md"), reverse=True)
    return [record(p, with_text=with_text) for p in paths]


def get(record_id: str) -> dict[str, Any] | None:
    """One record with its before/after text. `record_id` is a bare filename."""
    if "/" in record_id or "\\" in record_id or not record_id.endswith(".md"):
        return None
    path = PROMPT_HISTORY_DIR / record_id
    if not path.is_file():
        return None
    return record(path, with_text=True)


def summary() -> dict[str, Any]:
    recs = records()
    kept = [r for r in recs if r["kept"]]
    by_file: dict[str, int] = {}
    for r in kept:
        for f in r["files"]:
            by_file[f] = by_file.get(f, 0) + 1
    return {
        "attempted": len(recs),
        "live": len(kept),
        "reverted": len(recs) - len(kept),
        "live_by_file": by_file,
    }

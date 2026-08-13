"""Where the credentials are, and nothing else.

This used to hold seven directory constants pointing at one business's folders — which is
most of how a general engine came to be a plumbing company. Those directories belong to
whichever project is being run now; see `bat/runtime/project.py`.

What is left is genuinely global: an API key belongs to the machine, not to a project.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve()


def dotenv() -> Path | None:
    """The nearest `.env` at or above this package.

    Searched upward rather than fixed, because the same checkout gets run from the
    repository root, from `business-agent-template/`, and from inside a project directory,
    and a fixed path is right in exactly one of those.
    """
    for directory in HERE.parents:
        candidate = directory / ".env"
        if candidate.exists():
            return candidate
    return None


def load_dotenv() -> None:
    """Read it into the environment. Anything already set wins — an exported key is a
    deliberate act and a file should not quietly override it."""
    found = dotenv()
    if found is None:
        return
    for raw in found.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")

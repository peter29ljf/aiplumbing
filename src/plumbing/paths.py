"""Project path resolution. Every module locates config and prompts through here."""

from __future__ import annotations

import os
from pathlib import Path

# src/plumbing/paths.py -> src/plumbing -> src -> <project root>
ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
AGENTS_DIR = ROOT / "agents"
SHARED_DIR = AGENTS_DIR / "_shared"
# The graph a customer actually talks to. `agents/` above is the older five-agent shape,
# still driven by the testkit suite.
FLOW_DIR = ROOT / "flow"
FLOW_RULES_DIR = FLOW_DIR / "rules"
FLOW_RUNS_DIR = FLOW_DIR / "runs"
PERSONAS_DIR = ROOT / "personas"
SCENARIOS_DIR = ROOT / "scenarios"
RUNS_DIR = ROOT / "runs"
PROMPT_HISTORY_DIR = ROOT / "prompt_history"


def load_dotenv() -> None:
    """Minimal .env loader, no extra dependency. Existing env vars win."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

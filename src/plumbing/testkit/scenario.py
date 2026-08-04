"""Loading and validating scenario files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from plumbing.paths import SCENARIOS_DIR

REQUIRED_KEYS = {"id", "customer", "expect"}


def load(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_absolute():
        candidates = [Path.cwd() / file_path, SCENARIOS_DIR / file_path]
        file_path = next((c for c in candidates if c.exists()), file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")

    data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Malformed scenario file: {file_path}")

    missing = REQUIRED_KEYS - set(data)
    if missing:
        raise ValueError(f"Scenario {file_path} is missing required keys: {sorted(missing)}")

    data.setdefault("suite", file_path.parent.name)
    data.setdefault("description", "")
    data.setdefault("world", {})
    data["_path"] = str(file_path)
    return data


def load_suite(suite: str) -> list[dict[str, Any]]:
    """Load every scenario in a suite directory, sorted by filename."""
    directory = SCENARIOS_DIR / suite
    if not directory.exists():
        raise FileNotFoundError(f"Suite directory not found: {directory}")
    files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    if not files:
        raise FileNotFoundError(f"Suite {suite} contains no scenario files")
    return [load(f) for f in files]


def load_all() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for directory in sorted(p for p in SCENARIOS_DIR.iterdir() if p.is_dir()):
        scenarios.extend(load_suite(directory.name))
    return scenarios

"""The gate that decides simulator vs real service.

Deliberately boring and fail-closed: anything unexpected means "use the simulator".
"""

from __future__ import annotations

import os
from typing import Any

from plumbing import config
from plumbing.paths import load_dotenv


class LiveToolUnavailable(RuntimeError):
    """A tool is marked live but its credentials or client library are missing."""


def _catalog() -> dict[str, Any]:
    try:
        return config.tool_catalog()
    except FileNotFoundError:
        return {}


def master_enabled() -> bool:
    return bool(_catalog().get("live_tools_enabled", False))


def is_live(tool_name: str) -> bool:
    """True only when the master switch is on AND this tool is marked live."""
    if not master_enabled():
        return False
    return (_catalog().get("statuses") or {}).get(tool_name) == "live"


def live_status() -> dict[str, Any]:
    """What is actually live right now — surfaced in the console."""
    catalog = _catalog()
    statuses = catalog.get("statuses") or {}
    master = bool(catalog.get("live_tools_enabled", False))
    marked = sorted(k for k, v in statuses.items() if v == "live")
    return {
        "master_switch": master,
        "tools_marked_live": marked,
        "effectively_live": marked if master else [],
        "note": "Master switch is off; everything runs against the simulator."
        if not master
        else "REAL services are reachable for the tools listed.",
    }


def require_env(*names: str) -> dict[str, str]:
    """Fetch credentials, or fail with a message that names what is missing."""
    load_dotenv()
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise LiveToolUnavailable(
            f"Missing credentials in .env: {', '.join(missing)}. "
            f"The tool is marked live but cannot reach the service."
        )
    return values

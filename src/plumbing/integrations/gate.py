"""The gate that decides simulator vs real service.

Deliberately boring and fail-closed: anything unexpected means "use the simulator".

**Which tools are live belongs to a machine, not to the code.** It used to live in
`config/tool_catalog.yaml`, which git tracks — so the production server carried two
modified lines, and every `git pull` was a chance to either conflict or silently revert
them. Silently is the dangerous half: Telegram notifications stop, technicians stop
getting jobs, and nothing anywhere reports an error.

So the environment wins when it says anything, the same way `.env` holds credentials:

    PLUMBING_LIVE_ENABLED=true
    PLUMBING_LIVE_TOOLS=telegram.send,sms.send

Set in the systemd unit on the server. The catalog in git stays all-mocked forever, which
means pulling code can no longer touch what is switched on.
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


ENV_MASTER = "PLUMBING_LIVE_ENABLED"
ENV_TOOLS = "PLUMBING_LIVE_TOOLS"


def _env_master() -> bool | None:
    """The master switch from the environment, or None when it says nothing.

    Only an explicit, recognised value counts. A typo must not read as "on" — being
    fail-closed matters more here than being forgiving.
    """
    load_dotenv()
    raw = os.environ.get(ENV_MASTER)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    return False


def _env_tools() -> set[str] | None:
    load_dotenv()
    raw = os.environ.get(ENV_TOOLS)
    if raw is None:
        return None
    return {name.strip() for name in raw.split(",") if name.strip()}


def master_enabled() -> bool:
    from_env = _env_master()
    if from_env is not None:
        return from_env
    return bool(_catalog().get("live_tools_enabled", False))


def is_live(tool_name: str) -> bool:
    """True only when the master switch is on AND this tool is marked live."""
    if not master_enabled():
        return False
    from_env = _env_tools()
    if from_env is not None:
        return tool_name in from_env
    return (_catalog().get("statuses") or {}).get(tool_name) == "live"


def live_status() -> dict[str, Any]:
    """What is actually live right now — surfaced in the console.

    Reports where each answer came from. A console that says a tool is live while the
    process believes otherwise is worse than no console: somebody reads it, believes the
    technician is being notified, and stops checking.
    """
    catalog = _catalog()
    statuses = catalog.get("statuses") or {}
    env_master = _env_master()
    env_tools = _env_tools()

    master = master_enabled()
    marked = sorted(env_tools) if env_tools is not None else sorted(
        k for k, v in statuses.items() if v == "live"
    )
    return {
        "master_switch": master,
        "master_switch_source": "env" if env_master is not None else "file",
        "tools_marked_live": marked,
        "tools_source": "env" if env_tools is not None else "file",
        "effectively_live": marked if master else [],
        "note": "Master switch is off; everything runs against the simulator."
        if not master
        else "REAL services are reachable for the tools listed.",
    }


def set_switches(*, enabled: bool, tools: list[str]) -> dict[str, Any]:
    """Turn services on or off **for this process only**, until it restarts.

    The console needs this and it is all the console can honestly have. What production
    reaches lives in the systemd unit on the server; a switch here that wrote a tracked
    file would put it back in git, which is the exact arrangement that let a `git pull`
    silently stop Telegram notifications with nothing anywhere reporting an error.

    So: nothing is persisted. Flipping this on and walking away is safe in the one way
    that matters — the next restart is simulated again.
    """
    os.environ[ENV_MASTER] = "true" if enabled else "false"
    os.environ[ENV_TOOLS] = ",".join(sorted(tools))
    return live_status()


# What each service needs before it can reach anything, and the library it needs it with.
# Checked without sending, so the console can show a light rather than a bill.
NEEDS = {
    "sms.send": (("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"), ""),
    "email.send": (("GMAIL_USER", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                    "GMAIL_REFRESH_TOKEN"), ""),
    "telegram.send": (("TELEGRAM_BOT_TOKEN",), ""),
    "calendar.find_slots": (("GOOGLE_CALENDAR_ID",), "googleapiclient"),
    "calendar.create_appointment": (("GOOGLE_CALENDAR_ID",), "googleapiclient"),
}


def preflight(gate_key: str) -> str:
    """What stands between this service and working, or "" if nothing does.

    Credentials and imports only — nothing is sent and nothing is billed. It cannot prove
    a token is still valid, so a green light means "everything it needs is here", not
    "known good". Proving it takes `scripts/check_live.py`, which really sends.
    """
    load_dotenv()
    names, module = NEEDS.get(gate_key, ((), ""))
    missing = [name for name in names if not os.environ.get(name)]
    if gate_key.startswith("calendar.") and not (
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    ):
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
    if missing:
        return f"missing {', '.join(missing)}"
    if module:
        import importlib.util  # noqa: PLC0415

        if importlib.util.find_spec(module) is None:
            return f"{module} is not installed"
    return ""


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

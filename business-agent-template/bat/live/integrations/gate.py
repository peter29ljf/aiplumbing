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

Set in the systemd unit on the server, or in a `.env` beside the project.

**The environment is now the only source.** The generation this file came from kept the
catalog as a fallback and then made it all-mocked forever to stop pulls from touching it —
at which point it was a second answer to a question that already had one. It cost a real
outage the other way round too: the console read the file, production read the environment,
and a screen showing every tool mocked sat in front of a system that was sending live
texts. One source cannot disagree with itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Where `.env` is looked for: the project root, three directories up from here.
ROOT = Path(__file__).resolve().parents[4]


class LiveToolUnavailable(RuntimeError):
    """A tool is marked live but its credentials or client library are missing."""


def load_dotenv(path: Path | None = None) -> None:
    """`KEY=value` lines into the environment, never overwriting what is already set.

    Never overwriting is the important half: the systemd unit and an operator's shell both
    have to win over a file that somebody's editor may have half-saved.
    """
    where = path or ROOT / ".env"
    try:
        lines = where.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


ENV_MASTER = "PLUMBING_LIVE_ENABLED"
ENV_TOOLS = "PLUMBING_LIVE_TOOLS"

# Every name this switch answers to. The authority, and the reason it exists:
#
# `is_live` is a string lookup, and a name nobody recognises is not an error — it is
# silently, permanently mocked. An earlier tree registered `calendar.create` while this
# gate knew `calendar.create_appointment`; every booking would have stayed simulated
# forever and no log line anywhere would have mentioned it. A typo in a systemd unit does
# the same thing, on a machine nobody is watching.
#
# So an unrecognised name is refused out loud instead. `bat/live/world.py` names the ones
# it uses and `tests/test_gate_vocabulary.py` checks the two agree.
KNOWN_TOOLS = frozenset({
    "calendar.find_slots",
    "calendar.create_appointment",
    "calendar.reschedule",
    "calendar.cancel",
    "sms.send",
    "email.send",
    "telegram.send",
    "payment.send_deposit_link",
    "payment.check_status",
})


class UnknownLiveTool(ValueError):
    """A name in PLUMBING_LIVE_TOOLS that nothing will ever ask about."""


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
    """The names switched on, or None when the variable is not set at all.

    Set-but-empty is not the same as unset: `PLUMBING_LIVE_TOOLS=""` is somebody turning
    everything off, and it has to work.
    """
    load_dotenv()
    raw = os.environ.get(ENV_TOOLS)
    if raw is None:
        return None
    names = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = sorted(names - KNOWN_TOOLS)
    if unknown:
        raise UnknownLiveTool(
            f"{ENV_TOOLS} names {unknown}, which nothing ever asks about — so those would "
            f"stay mocked with nothing reporting it. Known names: {sorted(KNOWN_TOOLS)}"
        )
    return names


def master_enabled() -> bool:
    """Nothing is live until this says so, whatever the tool list holds."""
    return _env_master() is True


def is_live(tool_name: str) -> bool:
    """True only when the master switch is on AND this tool is named.

    Two switches rather than one because they answer different questions — "is this
    machine allowed to reach the outside world at all" and "which parts of it" — and
    because turning everything off in a hurry should be one word, not a list to edit.
    """
    if not master_enabled():
        return False
    return tool_name in (_env_tools() or set())


def live_status() -> dict[str, Any]:
    """What is actually live right now. Read before every stage of a rollout.

    The whole reason this exists: a screen that says a tool is mocked while the process
    is sending real texts is worse than no screen at all — somebody reads it, believes
    nothing is going out, and stops checking.
    """
    master = master_enabled()
    marked = sorted(_env_tools() or ())
    return {
        "master_switch": master,
        "tools_marked_live": marked,
        "effectively_live": marked if master else [],
        "source": f"{ENV_MASTER} and {ENV_TOOLS}",
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

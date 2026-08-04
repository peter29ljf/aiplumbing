"""Run-status broadcast: write "who is working right now" to runs/live.json for the console.

Deliberately a single JSON file rather than shared memory — the console is a separate
process from the one running scenarios, and a file is the simplest reliable channel.
A failed write never disturbs the run.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from plumbing.paths import RUNS_DIR

LIVE_FILE = RUNS_DIR / "live.json"

_state: dict[str, Any] = {
    "running": False,
    "scenario_id": "",
    "suite": "",
    "started_at": 0.0,
    "updated_at": 0.0,
    "active_agent": "",
    "phase": "idle",          # idle | running | judging | done
    "turn": 0,
    "agents": {},             # agent -> {turns, tool_calls, last_active}
    "last_tool": "",
    "last_message": "",
    "result": "",             # pass | fail | ""
}


def _enabled() -> bool:
    """Never write during pytest — unit tests must not pollute the console's live view."""
    return "PYTEST_CURRENT_TEST" not in os.environ and not os.environ.get(
        "PLUMBING_NO_LIVESTATUS"
    )


def _flush() -> None:
    if not _enabled():
        return
    _state["updated_at"] = time.time()
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LIVE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, LIVE_FILE)
    except OSError:
        pass  # a broken status broadcast must never break the run


def start_run(scenario_id: str, suite: str = "") -> None:
    _state.update(
        running=True,
        scenario_id=scenario_id,
        suite=suite,
        started_at=time.time(),
        active_agent="",
        phase="running",
        turn=0,
        agents={},
        last_tool="",
        last_message="",
        result="",
    )
    _flush()


def set_active(agent: str) -> None:
    _state["active_agent"] = agent
    entry = _state["agents"].setdefault(
        agent, {"turns": 0, "tool_calls": 0, "last_active": 0.0}
    )
    entry["last_active"] = time.time()
    _flush()


def record_turn(agent: str, message: str = "") -> None:
    entry = _state["agents"].setdefault(
        agent, {"turns": 0, "tool_calls": 0, "last_active": 0.0}
    )
    entry["turns"] += 1
    entry["last_active"] = time.time()
    _state["turn"] += 1
    if message:
        _state["last_message"] = message[:200]
    _flush()


def record_tool(agent: str, tool: str) -> None:
    entry = _state["agents"].setdefault(
        agent, {"turns": 0, "tool_calls": 0, "last_active": 0.0}
    )
    entry["tool_calls"] += 1
    entry["last_active"] = time.time()
    _state["last_tool"] = tool
    _flush()


def set_phase(phase: str) -> None:
    _state["phase"] = phase
    _flush()


def finish_run(result: str) -> None:
    _state.update(running=False, phase="done", active_agent="", result=result)
    _flush()


def read() -> dict[str, Any]:
    """Read by the console. Returns an idle state if the file is missing or corrupt."""
    try:
        return json.loads(LIVE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"running": False, "phase": "idle", "agents": {}, "active_agent": ""}

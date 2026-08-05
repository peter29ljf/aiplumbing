"""Tool registry.

Tools are named "namespace.action" (e.g. crm.lookup_by_phone). That name is what
configuration, scenario assertions and logs use. When the schema is sent to the model the
dot becomes an underscore, because OpenAI function names may not contain dots.

Each agent only sees the tools allow-listed in config/agents.yaml. That is where
permission isolation lives, and it is also what keeps each agent's prompt short: fewer
tools means fewer flows the prompt has to explain.
"""

from __future__ import annotations

import fnmatch
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from plumbing.world import ToolRejection, World


@dataclass
class ToolContext:
    """Everything a tool can touch during one scenario run."""

    world: World
    agent_name: str = ""
    # Which agents this deployment runs, when it runs only some. Left unset by the test
    # rig on purpose: a scenario suite that only exercised what production happens to
    # have switched on would quietly stop covering the rest.
    enabled_agents: tuple[str, ...] | None = None
    # Human simulators, injected by the runner. Tools do not care whether they are
    # LLM-driven or scripted.
    technician_sim: Callable[..., dict[str, Any]] | None = None
    supervisor_sim: Callable[..., dict[str, Any]] | None = None
    # Control signals read by the agent loop
    handoff_request: dict[str, Any] | None = None
    conversation_ended: bool = False
    end_reason: str = ""
    scenario: dict[str, Any] = field(default_factory=dict)
    # Called with each tool name just after it runs, when somebody is waiting to be told
    # what is happening. After rather than before on purpose: the tools themselves are
    # local and finish instantly, and the wait is the model call that follows — so the
    # name of the tool that just ran is what should be on screen during it. A customer sitting through a minute of tool calls sees three
    # dots and cannot tell a system that is working from one that has died; this is how
    # the widget says "checking the calendar" instead. Unset everywhere else, including
    # the test rig, where nobody is watching.
    progress: Callable[[str], None] | None = None


ToolHandler = Callable[..., Any]


@dataclass
class Tool:
    namespace: str
    action: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    @property
    def name(self) -> str:
        return f"{self.namespace}.{self.action}"

    @property
    def wire_name(self) -> str:
        return f"{self.namespace}_{self.action}"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.wire_name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_REGISTRY: dict[str, Tool] = {}


def tool(
    name: str, description: str, parameters: dict[str, Any] | None = None
) -> Callable[[ToolHandler], ToolHandler]:
    """Register a tool. `name` takes the form 'crm.lookup_by_phone'."""

    def decorator(handler: ToolHandler) -> ToolHandler:
        namespace, _, action = name.partition(".")
        if not namespace or not action:
            raise ValueError(f"Tool name must be 'namespace.action': {name}")
        if name in _REGISTRY:
            raise ValueError(f"Tool registered twice: {name}")
        _REGISTRY[name] = Tool(
            namespace=namespace,
            action=action,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            handler=handler,
        )
        return handler

    return decorator


def all_tools() -> dict[str, Tool]:
    _ensure_loaded()
    return dict(_REGISTRY)


def catalog() -> list[dict[str, Any]]:
    """Every tool the system knows about, registered or merely planned.

    Registered tools default to `mocked`; config/tool_catalog.yaml can promote one to
    `live` once its real adapter is wired up. Planned entries come from the same file
    and are not callable.
    """
    from plumbing import config  # noqa: PLC0415 - avoid a circular import at module load

    _ensure_loaded()
    try:
        cfg = config.tool_catalog()
    except FileNotFoundError:
        cfg = {}
    overrides = cfg.get("statuses") or {}

    entries: list[dict[str, Any]] = []
    for name, item in sorted(_REGISTRY.items()):
        entries.append(
            {
                "name": name,
                "namespace": item.namespace,
                "action": item.action,
                "description": item.description,
                "status": overrides.get(name, "mocked"),
                "parameters": sorted(
                    (item.parameters or {}).get("properties", {}).keys()
                ),
                "required": list((item.parameters or {}).get("required", [])),
                "grantable": True,
            }
        )

    for item in cfg.get("planned") or []:
        name = item.get("name", "")
        namespace = name.partition(".")[0]
        entries.append(
            {
                "name": name,
                "namespace": namespace,
                "action": name.partition(".")[2],
                "description": (item.get("description") or "").strip(),
                "status": "planned",
                "note": (item.get("note") or "").strip(),
                "parameters": [],
                "required": [],
                "grantable": False,
            }
        )

    return entries


def resolve(patterns: list[str]) -> list[Tool]:
    """Expand an allow-list (supporting 'crm.*') into concrete tools, order preserved."""
    _ensure_loaded()
    selected: dict[str, Tool] = {}
    for pattern in patterns:
        matched = [
            t for name, t in _REGISTRY.items() if fnmatch.fnmatchcase(name, pattern)
        ]
        if not matched:
            raise ValueError(
                f"Allow-list pattern '{pattern}' matched no tools. "
                f"Available: {sorted(_REGISTRY)}"
            )
        for item in matched:
            selected[item.name] = item
    return list(selected.values())


def schemas(tools: list[Tool]) -> list[dict[str, Any]]:
    return [t.schema() for t in tools]


def dispatch(
    ctx: ToolContext, tools: list[Tool], wire_name: str, raw_arguments: str
) -> dict[str, Any]:
    """Execute one tool call, return the result for the model, and write the audit log.

    Exceptions are never raised upward — the agent needs to see the error and correct
    itself, and that is exactly the signal doctor uses to find the gap in the prompt.
    """
    world = ctx.world
    by_wire = {t.wire_name: t for t in tools}
    entry: dict[str, Any] = {
        "at": world.now().isoformat(),
        "agent": ctx.agent_name,
        "tool": wire_name,
        "ok": False,
    }

    target = by_wire.get(wire_name)
    if target is None:
        entry["error"] = f"Tool '{wire_name}' does not exist or you are not permitted to use it"
        world.tool_log.append(entry)
        world.record_violation("unknown_tool", entry["error"], wire_name)
        return {"ok": False, "error": entry["error"], "available": sorted(by_wire)}

    entry["tool"] = target.name
    try:
        arguments = json.loads(raw_arguments) if raw_arguments else {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        entry["error"] = f"Could not parse arguments: {exc}"
        entry["raw_arguments"] = raw_arguments
        world.tool_log.append(entry)
        return {"ok": False, "error": entry["error"]}

    entry["arguments"] = arguments
    try:
        result = target.handler(ctx, **arguments)
    except ToolRejection as exc:
        entry["error"] = str(exc)
        entry["rejected"] = True
        world.tool_log.append(entry)
        if exc.violation:
            world.record_violation(exc.violation, str(exc), target.name)
        return {"ok": False, "error": str(exc)}
    except TypeError as exc:
        entry["error"] = f"Argument mismatch: {exc}"
        world.tool_log.append(entry)
        return {
            "ok": False,
            "error": f"Argument mismatch: {exc}",
            "expected_parameters": target.parameters,
        }
    except Exception as exc:  # noqa: BLE001 - internal errors go back to the agent too
        entry["error"] = f"{type(exc).__name__}: {exc}"
        world.tool_log.append(entry)
        return {"ok": False, "error": entry["error"]}

    payload = result if isinstance(result, dict) else {"result": result}
    entry["ok"] = True
    entry["result"] = payload
    world.tool_log.append(entry)
    return {"ok": True, **payload}


_loaded = False
_load_lock = threading.Lock()


def _ensure_loaded() -> None:
    """Import the tool implementation modules lazily, to avoid a circular import.

    Guarded by a lock and double-checked. Scenarios run in parallel, and setting the flag
    before the imports finish would let a second thread resolve against a half-populated
    registry — which fails as "pattern matched no tools" for whatever had not registered yet.
    """
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        from plumbing.tools import (  # noqa: F401
            comms_tools,
            info_tools,
            job_tools,
            ops_tools,
        )

        _loaded = True

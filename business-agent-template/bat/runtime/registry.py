"""The tool registry: how a tool is declared, listed, and called.

Machinery only. Not one business rule lives here — the tools themselves are in
`bat/presets/tools/` (the ones that come with the kit) and in a project's own `tools/`
directory (the ones it wrote for itself). This module is what both of them import.

Split out of what used to be one file holding both. A generator that writes new tools has
to be able to read the tools without reading the plumbing that registers them, and a
person auditing "what can this agent actually do" wants the same separation.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from bat.runtime.world import AnyWorld, Refused

Handler = Callable[..., Any]
_TOOLS: dict[str, dict[str, Any]] = {}


class NoToolsRegistered(Exception):
    """A file in a project's tools/ that defined nothing the registry can see.

    Worth its own error because of how it presents otherwise: the graph then fails with
    "wants the tool 'x', which does not exist", which reads like a typo in flow.yaml and
    sends whoever wrote it to the wrong file. A generated project once wrote six modules
    in tools/ trying to work out why, and the answer was a missing decorator.

    A genuine helper module belongs in `_something.py` — the loader skips those.
    """



def tool(name: str, description: str, properties: dict[str, Any],
         required: list[str] | None = None,
         remembers: tuple[str, ...] = ()) -> Callable[[Handler], Handler]:
    """`remembers` names the facts this tool handles that belong on the ticket.

    They are copied there by the engine, from the arguments and from the answer, without
    the model being asked to write them down as well. It was asked, once: a customer gave
    their number, the lookup used it, the step ended, the number went with the messages,
    and the next step asked for it again. Being asked twice for the same thing is the
    clearest sign nobody is listening, and it should not depend on diligence.
    """
    def register(handler: Handler) -> Handler:
        _TOOLS[name] = {
            "name": name,
            "handler": handler,
            "remembers": remembers,
            "schema": {
                "type": "function",
                "function": {
                    "name": name.replace(".", "_", 1),
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required or list(properties),
                    },
                },
            },
        }
        return handler

    return register


def names() -> set[str]:
    return set(_TOOLS)


def schemas_for(wanted: tuple[str, ...], *,
               outcomes: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """The node's tools. `step.finished` is given that node's own ways out as an enum."""
    import copy

    built = []
    for name in wanted:
        if name not in _TOOLS:
            continue
        schema = _TOOLS[name]["schema"]
        if name == "step.finished" and outcomes:
            schema = copy.deepcopy(schema)
            field = schema["function"]["parameters"]["properties"]["outcome"]
            field["enum"] = list(outcomes)
            field["description"] = "Which way this step came out."
        built.append(schema)
    return built


def call(world: AnyWorld, wire_name: str, arguments: str,
         allowed: tuple[str, ...]) -> tuple[Any, dict[str, Any]]:
    """Run one tool call, and say what it learned that outlives this step.

    `allowed` is the node's list — a node cannot reach past it. The second return value is
    what belongs on the ticket, taken from the tool's `remembers`.
    """
    name = next((n for n in allowed if _TOOLS.get(n, {}).get("schema", {})
                 .get("function", {}).get("name") == wire_name), None)
    if name is None:
        return {"ok": False,
                "error": f"'{wire_name}' is not available here. You can use: {list(allowed)}"}, {}

    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError as bad:
        return {"ok": False, "error": f"Those arguments are not JSON: {bad}"}, {}

    try:
        result = _TOOLS[name]["handler"](world, **args)
    except Refused as refusal:
        return {"ok": False, "error": str(refusal)}, {}
    except TypeError as bad_args:
        return {"ok": False, "error": f"Wrong arguments for {name}: {bad_args}"}, {}

    keep: dict[str, Any] = {}
    for key in _TOOLS[name]["remembers"]:
        value = args.get(key, result.get(key) if isinstance(result, dict) else None)
        if value not in (None, "", [], {}):
            keep[key] = value
    return result, keep


def _ticket(world: AnyWorld, ticket_id: str):
    """The ticket by id, or a refusal naming the ones that exist.

    Generic rather than per-tool: every tool that writes anything needs it, and a model
    that invents a ticket id should get told which ones are real instead of a stack trace.
    """
    found = world.tickets.get(ticket_id)
    if found is None:
        raise Refused(f"No ticket '{ticket_id}'. Open ones: {sorted(world.tickets)}")
    return found


def load_tools(project: Any = None) -> set[str]:
    """Import the kit's tools, then any the project wrote for itself, and say what exists.

    Importing is what registers: `@tool` runs at import time. A project that names a tool
    in flow.yaml and never gets its module imported fails validation with "that tool does
    not exist", which is a confusing way to describe a missing import — so this is called
    before the graph is loaded, never after.
    """
    import importlib
    import importlib.util

    complaints: list[str] = []
    importlib.import_module("bat.presets.tools.service")

    if project is not None and project.tools_dir.exists():
        for module_path in sorted(project.tools_dir.glob("*.py")):
            if module_path.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(
                f"bat_project_{project.name}_{module_path.stem}", module_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                before = set(_TOOLS)
                spec.loader.exec_module(module)
                if set(_TOOLS) == before:
                    # A file in tools/ that registered nothing. Almost always a plain
                    # function somebody expected to be a tool — which then fails
                    # validation as "that tool does not exist", reads like a typo, and
                    # sends whoever wrote it looking in flow.yaml. Say it where it
                    # happened.
                    complaints.append(
                        f"{module_path.name} registered no tools. A function in tools/ "
                        f"only becomes one with the @tool decorator from "
                        f"bat.runtime.registry."
                    )
    if complaints:
        raise NoToolsRegistered("\n  - ".join(["", *complaints]))
    return names()

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

from bat.runtime.graph import BrokenFlow
from bat.runtime.world import AnyWorld, Refused

Handler = Callable[..., Any]
_TOOLS: dict[str, dict[str, Any]] = {}


COMPLAINTS: list[str] = []


class NoToolsRegistered(BrokenFlow):
    """A file in a project's tools/ that defined nothing the registry can see.

    A `BrokenFlow` because that is what it is — the project does not hang together — and
    because every caller already handles that. Raised as its own type it escaped the one
    place that mattered: the command that feeds the loader's complaint back to the builder
    caught `BrokenFlow`, missed this, and crashed instead of saying what was wrong.

    Worth its own error because of how it presents otherwise: the graph then fails with
    "wants the tool 'x', which does not exist", which reads like a typo in flow.yaml and
    sends whoever wrote it to the wrong file. A generated project once wrote six modules
    in tools/ trying to work out why, and the answer was a missing decorator.

    A genuine helper module belongs in `_something.py` — the loader skips those.
    """



def tool(name: str, description: str, properties: dict[str, Any],
         required: list[str] | None = None,
         remembers: tuple[str, ...] = (),
         once: bool = False) -> Callable[[Handler], Handler]:
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
            "once": once,
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


def reaches_outside(name: str) -> bool:
    """Does this tool touch a diary, a phone, or a person? Then doing it is the work.

    The closing gate asks this of a last step's tools. Anything that only reads — a
    price, the clock, what is free — is never the reason a step exists, and requiring it
    turns a granted tool into a compulsory one.
    """
    return bool(_TOOLS.get(name, {}).get("once"))


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
        # A last step has no `step.finished` — it ends by replying, and its list says so.
        # But an engine that shows every tool in the graph (one session, one MCP server)
        # lets the model reach for it anyway, and "not available here" reads as an error
        # to retry. One did, three times, and the scenario was failed for going round in
        # circles. It was not confused about what it wanted; it was told no without being
        # told what instead.
        if wire_name in ("step_finished", "step.finished"):
            return {"ok": False,
                    "error": "This is the last step, so there is nothing to finish into. "
                             "Do whatever this step still has to do, then write the "
                             "message the customer is left with — your reply is what ends "
                             "the conversation."}, {}
        return {"ok": False,
                "error": f"'{wire_name}' is not available here. You can use: {list(allowed)}"}, {}

    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError as bad:
        return {"ok": False, "error": f"Those arguments are not JSON: {bad}"}, {}

    # An action that reaches outside the process, already done with these arguments. Hand
    # back what it answered the first time and touch nothing: the caller cannot tell, which
    # is the whole idea. The attempt is recorded because a step that had to be stopped from
    # booking twice is a step worth looking at.
    done = getattr(world, "done", None)
    key = ""
    if _TOOLS[name].get("once") and done is not None:
        key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if key in done:
            world.repeats.append({"tool": name, "arguments": args})
            return done[key], {}

    try:
        result = _TOOLS[name]["handler"](world, **args)
    except Refused as refusal:
        # Not remembered: it did not happen, so a step that fixes its arguments and calls
        # again must be allowed to. Remembering a refusal would hand the same refusal back
        # forever.
        return {"ok": False, "error": str(refusal)}, {}
    except TypeError as bad_args:
        return {"ok": False, "error": f"Wrong arguments for {name}: {bad_args}"}, {}

    if key and done is not None:
        done[key] = result

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
                    complaints.append(
                        f"{module_path.name} registered no tools — a function in tools/ "
                        f"only becomes one with the @tool decorator from "
                        f"bat.runtime.registry"
                    )

    # Recorded, not raised. Raising stopped three projects that were running perfectly
    # well: a junk file in tools/ is only a problem if flow.yaml actually wanted a tool
    # from it, and refusing to start otherwise is the check breaking the thing it was
    # added to protect. `graph._check` picks these up and attaches them to the missing
    # tool it is already reporting, which is where somebody is looking anyway.
    COMPLAINTS[:] = complaints
    return names()


def complaints() -> list[str]:
    """Why a tools/ file might not have given you what you expected."""
    return list(COMPLAINTS)

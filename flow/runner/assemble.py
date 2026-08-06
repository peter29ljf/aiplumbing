"""Build one node's system prompt.

Four parts, in this order: what is always true, what this node is for, the rules that
apply here, and what the conversation has already settled. Nothing about the other twelve
nodes — a node does not know they exist and cannot be tempted to do their work.

The rules are read from disk and put in the prompt when it is built, rather than fetched
by the model when it gets there. The context saving is identical and it costs one round
trip fewer, and a round trip is five to eighteen seconds against a twenty-second budget.

Assembled sizes are the acceptance test for the whole rewrite: the old agents send 42,968
characters on every call. `report()` prints what each node actually costs, so a node that
grows back cannot do it quietly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from flow.runner.graph import Flow, Node
from flow.runner.memory import summarise

FLOW_DIR = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=64)
def _read(relative: str) -> str:
    return (FLOW_DIR / relative).read_text(encoding="utf-8").strip()


def always() -> str:
    return _read("always.md")


def build(node: Node, *, tags: dict | None = None, ticket_id: str = "") -> str:
    """The system prompt for one node."""
    parts = [always(), f"# Right now\n\n{node.goal}"]

    for rule in node.rules:
        parts.append(_read(f"rules/{rule}.md"))

    # Every node says how it ends, including the ones with only one way on. Nothing
    # infers that a step is finished from what was said — inferring it is how a flow ends
    # up somewhere nobody can explain.
    if node.is_terminal:
        parts.append(
            "# Finishing\n\n"
            "When there is nothing left to do, say your closing words to the customer and "
            "call `conversation.end` in the same turn."
        )
    else:
        # A step with one way on and a step choosing between several are not in the same
        # position, and telling both to "be sure first" was read as licence to keep going:
        # the greeting node held a customer for seven exchanges, collecting things three
        # other nodes exist to collect.
        if node.choices:
            named = ", ".join(f"`{choice}`" for choice in node.choices)
            parts.append(
                "# When this step is done\n\n"
                f"Call `step.finished` with `outcome` set to one of {named}.\n\n"
                "Be sure before you choose — one more question is cheaper than sending "
                "somebody down the wrong path, and you cannot come back."
            )
        else:
            parts.append(
                "# When this step is done\n\n"
                "Call `step.finished` with `outcome` set to `done`.\n\n"
                "**As soon as this step's goal is met, and in the same turn.** Not when "
                "the customer's problem is solved — that takes several more steps and they "
                "are not yours. Anything you are curious about that is not named above "
                "belongs to somebody else, and they will ask."
            )

    parts.append("# What is already known\n\n" + summarise(tags or {}, ticket_id=ticket_id))
    return "\n\n---\n\n".join(parts)


def report(flow: Flow, schemas_for) -> list[dict]:
    """What every node costs, biggest first. The number this rewrite is judged on."""
    import json

    rows = []
    for node in flow.nodes.values():
        prompt = build(node)
        schemas = json.dumps(schemas_for(node.tools))
        rows.append({
            "node": node.name,
            "prompt": len(prompt),
            "tools": len(node.tools),
            "schemas": len(schemas),
            "total": len(prompt) + len(schemas),
        })
    return sorted(rows, key=lambda row: -row["total"])

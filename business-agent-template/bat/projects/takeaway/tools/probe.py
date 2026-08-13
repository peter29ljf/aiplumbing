from bat.runtime.registry import tool, _ticket
from bat.runtime.world import AnyWorld


@tool(
    "probe.world",
    "Probe the world interface.",
    {"ticket_id": {"type": "string"}},
)
def probe_world(world: AnyWorld, ticket_id: str) -> dict:
    ticket = _ticket(world, ticket_id)
    methods = [m for m in dir(world) if not m.startswith("__")]
    ticket.tags["probed_methods"] = ", ".join(methods)
    return {"methods": methods}
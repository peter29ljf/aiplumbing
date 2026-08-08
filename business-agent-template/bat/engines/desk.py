"""The world stays here. Claude Code reaches it over a loopback socket.

The probe kept its world in a JSON file, which worked because it had four toy tools. The
real one cannot: `sim.World` is a live object graph with tickets, a diary, technicians and
a clock, it has `snapshot()` but nothing that reads a snapshot back, and the judging, the
delta detector and the follow-up loop all hold a reference to the same instance the tools
mutate. Serialising it per call would be a second implementation of the world, which is
exactly the thing the two-backend design exists to avoid.

So the world does not move. This process keeps it, and each `claude` subprocess gets a
stdio MCP server (`mcp_proxy.py`) that owns no state at all and forwards every call here.
One HTTP request per tool call, on 127.0.0.1, to a port that only exists while the suite
is running.

Concurrency is by conversation: `/call` names one, and each has its own world, so ten
scenarios in flight never touch each other's tickets. `registry.call` is what actually
runs — the same function the in-process engine calls, with the same `allowed` list and
the same `remembers` — so a tool cannot behave differently depending on which engine is
driving it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from bat.runtime import registry


class Desk:
    """Which world belongs to which conversation, and how a call reaches it."""

    def __init__(self) -> None:
        self._worlds: dict[str, Any] = {}
        self._tickets: dict[str, str] = {}
        self._allowed: dict[tuple[str, str], tuple[str, ...]] = {}
        self._seen: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self.endpoint = ""

    def register(self, conversation: str, world: Any, ticket_id: str) -> None:
        with self._lock:
            self._worlds[conversation] = world
            self._tickets[conversation] = ticket_id

    def ticket_is_now(self, conversation: str, ticket_id: str) -> None:
        """A customer who comes back gets a fresh ticket, and `remembers` has to follow
        it. Nothing else changes."""
        with self._lock:
            self._tickets[conversation] = ticket_id

    def drain(self, conversation: str) -> list[dict[str, Any]]:
        """What the tools did since this was last asked.

        The driver learns from here that a step called `step.finished`, and with which
        way out. Reading it off the tool results is the same source the in-process engine
        uses; parsing it back out of the model's text would be a second, worse answer to
        a question already settled.
        """
        with self._lock:
            return self._seen.pop(conversation, [])

    def forget(self, conversation: str) -> None:
        with self._lock:
            self._worlds.pop(conversation, None)
            for key in [k for k in self._allowed if k[0] == conversation]:
                self._allowed.pop(key, None)

    def entering(self, conversation: str, node: str, allowed: tuple[str, ...]) -> None:
        """A node's tool list, recorded so a call can be checked against the node it came
        from rather than against everything the project owns."""
        with self._lock:
            self._allowed[(conversation, node)] = allowed

    def call(self, conversation: str, node: str, wire_name: str,
             arguments: str) -> tuple[Any, dict[str, Any]]:
        with self._lock:
            world = self._worlds.get(conversation)
            allowed = self._allowed.get((conversation, node), ())
        if world is None:
            return {"ok": False, "error": f"no world for conversation {conversation}"}, {}
        # The lock is not held here. A tool can take a while, and two conversations
        # holding each other up would turn ten parallel scenarios into one queue; each
        # world is only ever touched by its own conversation, so there is nothing to
        # guard once the reference is in hand.
        result, keep = registry.call(world, wire_name, arguments, allowed)

        with self._lock:
            ticket_id = self._tickets.get(conversation, "")
            # `remembers` is applied here rather than being handed back for the driver to
            # apply, because the driver is on the other side of a subprocess and the
            # whole point of the mechanism is that nobody has to remember to do it.
            if keep and ticket_id and ticket_id in world.tickets:
                world.tickets[ticket_id].tags.update(keep)
            self._seen.setdefault(conversation, []).append(
                {"node": node, "tool": wire_name, "result": result, "keep": keep})
        return result, keep


class _Handler(BaseHTTPRequestHandler):
    desk: Desk

    def do_POST(self) -> None:  # noqa: N802 - the base class names it
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as bad:
            return self._send({"result": {"ok": False, "error": f"bad request: {bad}"},
                               "keep": {}})

        if self.path == "/entering":
            self.desk.entering(payload["conversation"], payload["node"],
                               tuple(payload.get("allowed") or ()))
            return self._send({"ok": True})

        result, keep = self.desk.call(
            payload.get("conversation", ""), payload.get("node", ""),
            payload.get("tool", ""), json.dumps(payload.get("arguments") or {}))
        self._send({"result": result, "keep": keep})

    def _send(self, body: dict) -> None:
        raw = json.dumps(body, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_: Any) -> None:
        """Silent. One line per tool call across ten conversations buries the report."""


def serve(desk: Desk) -> tuple[str, ThreadingHTTPServer]:
    """Start on a port the operating system picks, and say where it is."""
    handler = type("Handler", (_Handler,), {"desk": desk})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    desk.endpoint = f"http://{host}:{port}"
    return desk.endpoint, server

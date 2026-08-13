"""One node's tools, as an MCP server. Holds nothing; forwards everything.

Started by `claude -p` over stdio, once per node. Everything it needs arrives in the
environment: which conversation it belongs to, which node, where the world is listening,
and the tool schemas that node is allowed — written to a file rather than passed as an
argument, because fifteen schemas do not belong on a command line.

**The tool list is the node's tool list, and that is the point.** The probe run that
preceded this tried to do it with `--allowedTools` and watched a step with no business
booking anything be offered the booking tool anyway: that flag is a permission list, not
a filter on what exists. Here the filter is structural — a tool the node was not given
is never registered, so it is not there to be called, and no wording in a prompt has to
hold the line.

The schemas come from `registry.schemas_for`, which is also what the in-process engine
uses, so `step.finished` arrives carrying this node's own ways out as an enum. A probe
conversation was told in as many words to mark a ticket "escalated" from a step with no
such exit, and could not: it took one of the three it had.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

ENDPOINT = os.environ["FLOW_ENDPOINT"]
CONVERSATION = os.environ["FLOW_CONVERSATION"]
NODE = os.environ["FLOW_NODE"]
SCHEMAS = json.loads(open(os.environ["FLOW_SCHEMAS"]).read())

server = Server("flow")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=schema["function"]["name"],
            description=schema["function"]["description"],
            inputSchema=schema["function"]["parameters"],
        )
        for schema in SCHEMAS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    body = json.dumps({
        "conversation": CONVERSATION, "node": NODE,
        "tool": name, "arguments": arguments or {},
    }).encode()
    request = urllib.request.Request(f"{ENDPOINT}/call", data=body,
                                     headers={"Content-Type": "application/json"})
    # Blocking, deliberately. The alternative is an async HTTP client as a dependency for
    # a request to loopback that returns in under a millisecond, and this server handles
    # one call at a time anyway — the model is waiting for the answer either way.
    answer = await asyncio.to_thread(
        lambda: json.loads(urllib.request.urlopen(request, timeout=120).read()))
    return [types.TextContent(type="text",
                              text=json.dumps(answer.get("result"), default=str))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

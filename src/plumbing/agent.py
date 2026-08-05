"""The generic agent loop.

An agent is a prompt, a set of tools, and a list of handoff targets. There is no business
logic in this file; swapping agents is only ever swapping a prompt and an allow-list.

Talking to the customer is not a tool: the model's plain text output IS the message sent
to the customer. When it needs a tool it calls the tool first, and writes its reply only
after the result comes back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from plumbing import livestatus
from plumbing.llm import LLM
from plumbing.tools import Tool, dispatch, schemas
from plumbing.tools.registry import ToolContext


@dataclass
class AgentSpec:
    name: str
    description: str
    system_prompt: str
    tools: list[Tool]
    handoff_to: list[str] = field(default_factory=list)
    is_stub: bool = False


@dataclass
class TurnResult:
    reply: str | None = None            # What to send to the customer
    ended: bool = False                 # The agent declared the process closed
    end_reason: str = ""
    handoff: dict[str, Any] | None = None
    tool_calls: list[str] = field(default_factory=list)
    stalled: bool = False               # Hit the per-turn tool-call ceiling without replying


class Agent:
    def __init__(self, spec: AgentSpec, llm: LLM) -> None:
        self.spec = spec
        self.llm = llm
        self._schemas = schemas(spec.tools)

    @property
    def name(self) -> str:
        return self.spec.name

    def initial_messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.spec.system_prompt}]

    def respond(self, ctx: ToolContext, messages: list[dict[str, Any]]) -> TurnResult:
        """Run one full agent turn: the tool-call loop, then one reply to the customer.

        `messages` is appended to in place (assistant and tool messages) so the next turn
        can reuse it.
        """
        ctx.agent_name = self.spec.name
        ctx.scenario["_handoff_targets"] = self.spec.handoff_to
        livestatus.set_active(self.spec.name)

        max_tool_calls = self.llm.limit("max_tool_calls_per_turn", 12)
        result = TurnResult()
        calls_used = 0

        while True:
            message = self.llm.chat(
                "agent", messages, tools=self._schemas if self._schemas else None
            )
            tool_calls = list(getattr(message, "tool_calls", None) or [])

            messages.append(_assistant_message(message, tool_calls))

            if not tool_calls:
                text = (message.content or "").strip()
                result.reply = text or None
                return result

            for call in tool_calls:
                calls_used += 1
                payload = dispatch(
                    ctx, self.spec.tools, call.function.name, call.function.arguments
                )
                result.tool_calls.append(call.function.name)
                livestatus.record_tool(self.spec.name, call.function.name)
                if ctx.progress is not None:
                    ctx.progress(call.function.name)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": _json(payload),
                    }
                )

            # A handoff or a close ends the turn immediately — whatever comes next is
            # not this agent's to say.
            if ctx.handoff_request is not None:
                result.handoff = ctx.handoff_request
                ctx.handoff_request = None
                return result
            if ctx.conversation_ended:
                result.ended = True
                result.end_reason = ctx.end_reason
                return result

            if calls_used >= max_tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "[system] You have reached the tool-call limit for this "
                        "turn. Reply to the customer in plain text now, without calling "
                        "any more tools.",
                    }
                )
                message = self.llm.chat("agent", messages)
                messages.append({"role": "assistant", "content": message.content or ""})
                result.reply = (message.content or "").strip() or None
                result.stalled = True
                return result


def _assistant_message(message: Any, tool_calls: list[Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    if tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in tool_calls
        ]
    return payload


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)

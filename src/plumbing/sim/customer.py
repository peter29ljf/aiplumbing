"""The simulated customer: played by an LLM, with personality and goals from the scenario.

Sampling at high temperature means the wording differs every run. That is the point of
"multi-scenario automated testing" — what is being tested is whether the agent handles a
real, off-script person, not whether it can replay a fixed transcript.
"""

from __future__ import annotations

from typing import Any

from plumbing.llm import LLM, LLMError
from plumbing.paths import PERSONAS_DIR


class CustomerSim:
    def __init__(self, llm: LLM, scenario: dict[str, Any]) -> None:
        self.llm = llm
        spec = scenario.get("customer", {}) or {}
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(spec)}
        ]
        self.turns = 0
        self.max_turns = int(spec.get("max_turns", 20))

    # ------------------------------------------------------------------
    def _system_prompt(self, spec: dict[str, Any]) -> str:
        template = (PERSONAS_DIR / "customer.md").read_text(encoding="utf-8")
        facts = spec.get("facts", {}) or {}
        fact_lines = (
            "\n".join(f"- {key}: {value}" for key, value in facts.items())
            or "- (No fixed facts; answer naturally from your situation.)"
        )
        rules = spec.get("behavior_rules", []) or [
            "Cooperate normally; answer what you are asked."
        ]
        ends = spec.get("end_conditions", []) or [
            "Once your question is clearly answered or the next step is arranged, thank them and stop."
        ]
        return (
            template.replace("{persona_card}", spec.get("persona", "").strip())
            .replace("{facts}", fact_lines)
            .replace("{behavior_rules}", "\n".join(f"- {r}" for r in rules))
            .replace("{end_conditions}", "\n".join(f"- {e}" for e in ends))
        )

    # ------------------------------------------------------------------
    def __call__(self, agent_message: str | None) -> dict[str, Any]:
        """`agent_message` is None for the opening line."""
        self.turns += 1
        if self.turns > self.max_turns:
            return {
                "text": "Sorry, I have to go. Thanks anyway.",
                "ended": True,
                "reason": "Customer hit the maximum number of turns",
            }

        if agent_message is None:
            self.messages.append(
                {
                    "role": "user",
                    "content": "[system] Write the first thing you say to this plumbing company.",
                }
            )
        else:
            self.messages.append({"role": "user", "content": agent_message})

        try:
            reply = self.llm.chat_json("customer", self.messages)
        except LLMError as exc:
            # Do not dress this up as the customer leaving. A flaky simulator that looks
            # like a normal ending turns harness noise into a verdict about the agent.
            return {
                "text": "",
                "ended": True,
                "error": True,
                "reason": f"Customer simulator failed: {exc}",
            }

        text = str(reply.get("text", "")).strip()
        ended = bool(reply.get("ended", False))
        if not text:
            text = "..."
        self.messages.append(
            {"role": "assistant", "content": _dump({"text": text, "ended": ended})}
        )
        return {"text": text, "ended": ended, "reason": str(reply.get("reason", ""))}


def _dump(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)

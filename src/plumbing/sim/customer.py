"""The simulated customer: played by an LLM, with personality and goals from the scenario.

Sampling at high temperature means the wording differs every run. That is the point of
"multi-scenario automated testing" — what is being tested is whether the agent handles a
real, off-script person, not whether it can replay a fixed transcript.

Output is plain text with an `[END]` marker rather than JSON. Asking for strict JSON at
temperature 0.9 across a forty-turn conversation was the single largest source of harness
noise: a quarter of end-to-end runs died on a malformed object, and a broken simulator
looks exactly like a customer hanging up. Plain text has nothing to malform.
"""

from __future__ import annotations

import re
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
            raw = self.llm.chat_text("customer", self.messages)
        except LLMError as exc:
            # Do not dress this up as the customer leaving. A flaky simulator that looks
            # like a normal ending turns harness noise into a verdict about the agent.
            return {
                "text": "",
                "ended": True,
                "error": True,
                "reason": f"Customer simulator failed: {exc}",
            }

        text, ended, reason = parse_reply(raw)
        self.messages.append({"role": "assistant", "content": raw})
        return {"text": text, "ended": ended, "reason": reason}


_END_MARKER = re.compile(r"^\s*\[END\]\s*(.*)$", re.IGNORECASE)


def parse_reply(raw: str) -> tuple[str, bool, str]:
    """Split a plain-text reply into the message, whether it ends, and why.

    Tolerant on purpose: the marker may be missing, on the same line, wrapped in a code
    fence, or written as [end]. Anything that is not a marker is what the customer said.
    """
    lines = [line for line in (raw or "").splitlines()]
    kept: list[str] = []
    ended = False
    reason = ""

    for line in lines:
        stripped = line.strip()
        if stripped in ("```", "```text"):
            continue
        match = _END_MARKER.match(stripped)
        if match:
            ended = True
            reason = match.group(1).strip()
            continue
        # A marker tacked onto the end of a sentence rather than its own line.
        if "[END]" in stripped.upper():
            index = stripped.upper().index("[END]")
            before, after = stripped[:index].strip(), stripped[index + 5 :].strip()
            if before:
                kept.append(before)
            ended = True
            reason = reason or after
            continue
        kept.append(line)

    text = "\n".join(kept).strip()
    if not text:
        # It ended without saying anything. Give it a goodbye rather than an empty turn.
        text = "Thanks, that's all I needed." if ended else "..."
    return text, ended, reason or ("customer ended the conversation" if ended else "")

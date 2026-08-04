"""The simulated supervisor, reviewing escalated complaints, disputes and refunds."""

from __future__ import annotations

from typing import Any

from plumbing.llm import LLM, LLMError
from plumbing.paths import PERSONAS_DIR
from plumbing.world import Ticket

_DEFAULT_POLICY = (
    "You go by the rules: approve what the rules allow, explain clearly when they do not, "
    "and ask for more material when the evidence is thin. You do not hand out compensation "
    "readily."
)


class SupervisorSim:
    def __init__(self, llm: LLM, scenario: dict[str, Any]) -> None:
        self.llm = llm
        self.policy = (scenario.get("supervisor", {}) or {}).get("policy", _DEFAULT_POLICY)
        self.template = (PERSONAS_DIR / "supervisor.md").read_text(encoding="utf-8")

    def __call__(self, ticket: Ticket, reason: str, details: str) -> dict[str, Any]:
        prompt = (
            self.template.replace("{ticket_id}", ticket.ticket_id)
            .replace("{ticket_status}", ticket.status)
            .replace("{reason}", reason)
            .replace("{details}", details)
            .replace("{supervisor_policy}", self.policy)
        )
        try:
            result = self.llm.chat_json(
                "supervisor",
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "Give your decision."},
                ],
            )
        except LLMError as exc:
            return {"decision": "pending", "notes": f"Supervisor unavailable: {exc}"}

        return {
            "decision": str(result.get("decision", "pending")),
            "notes": str(result.get("notes", "")),
        }

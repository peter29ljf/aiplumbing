"""Assemble agents from config/agents.yaml.

Adding an agent means adding a config block and writing a prompt file. Nothing here
knows anything about any specific agent's business.
"""

from __future__ import annotations

from typing import Any

from plumbing import config
from plumbing.agent import Agent, AgentSpec
from plumbing.llm import LLM
from plumbing.paths import AGENTS_DIR, SHARED_DIR
from plumbing.tools import resolve


def load_shared(name: str) -> str:
    path = SHARED_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Missing shared prompt fragment: {path}")
    return path.read_text(encoding="utf-8").strip()


def agent_prompt_path(agent_name: str, cfg: dict[str, Any] | None = None) -> Any:
    cfg = cfg or config.agents_config()
    spec = cfg["agents"][agent_name]
    return AGENTS_DIR / spec["prompt"]


def build_system_prompt(
    agent_name: str, cfg: dict[str, Any] | None = None,
    enabled: set[str] | None = None,
) -> str:
    """Shared fragments + the agent's own prompt + runtime context."""
    cfg = cfg or config.agents_config()
    spec = cfg["agents"][agent_name]

    parts = [load_shared(name) for name in spec.get("shared", [])]

    own_path = AGENTS_DIR / spec["prompt"]
    if not own_path.exists():
        raise FileNotFoundError(f"Missing agent prompt: {own_path}")
    parts.append(own_path.read_text(encoding="utf-8").strip())

    # Only agents this deployment actually runs. Advertising one that is switched off
    # invites a transfer that cannot land, and the model has no way to know. `enabled`
    # is None everywhere except a live deployment, so the test rig sees every target.
    targets = spec.get("handoff_to", [])
    if enabled is not None:
        targets = [t for t in targets if t in enabled]
    context = [
        "# Runtime context",
        f"You are currently acting as **{agent_name}** — {spec['description']}",
    ]
    if targets:
        lines = [
            f"- `{t}`: {cfg['agents'][t]['description']}"
            for t in targets
            if t in cfg["agents"]
        ]
        context.append("You may hand off to:\n" + "\n".join(lines))
    else:
        context.append(
            "You cannot hand this ticket to another agent; you must see the job through yourself."
        )
    parts.append("\n\n".join(context))

    return "\n\n---\n\n".join(part for part in parts if part)


def build_agent(
    agent_name: str, llm: LLM, cfg: dict[str, Any] | None = None,
    enabled: set[str] | None = None,
) -> Agent:
    cfg = cfg or config.agents_config()
    if agent_name not in cfg["agents"]:
        raise KeyError(
            f"No agent '{agent_name}' in config/agents.yaml. Registered: {sorted(cfg['agents'])}"
        )
    spec = cfg["agents"][agent_name]
    return Agent(
        AgentSpec(
            name=agent_name,
            description=spec["description"],
            system_prompt=build_system_prompt(agent_name, cfg, enabled),
            tools=resolve(spec.get("tools", [])),
            # The agent may only offer what this deployment runs. handoff.transfer
            # checks the same set, so the prompt and the tool cannot disagree.
            handoff_to=[
                t for t in spec.get("handoff_to", [])
                if enabled is None or t in enabled
            ],
            is_stub=bool(spec.get("stub", False)),
        ),
        llm,
    )


def build_all(
    llm: LLM, cfg: dict[str, Any] | None = None, enabled: set[str] | None = None,
) -> dict[str, Agent]:
    cfg = cfg or config.agents_config()
    return {name: build_agent(name, llm, cfg, enabled) for name in cfg["agents"]}


def entry_agent_name(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or config.agents_config()
    return cfg.get("entry_agent", "intake")

"""Configuration loading. Every YAML config is read here once and cached."""

from __future__ import annotations

import copy
from functools import lru_cache
from typing import Any

import yaml

from plumbing.paths import CONFIG_DIR


@lru_cache(maxsize=None)
def _load(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Config file must be a mapping: {path}")
    return data


def _fresh(name: str) -> dict[str, Any]:
    """Deep copy, so callers cannot mutate the cache."""
    return copy.deepcopy(_load(name))


def business_rules() -> dict[str, Any]:
    return _fresh("business_rules.yaml")


def ticket_states() -> dict[str, Any]:
    return _fresh("ticket_states.yaml")


def llm_config() -> dict[str, Any]:
    return _fresh("llm.yaml")


def agents_config() -> dict[str, Any]:
    return _fresh("agents.yaml")


def live_config() -> dict[str, Any]:
    """Which agents this deployment routes to. Absent file means everything is on.

    The test rig deliberately does not read this: a scenario suite that only exercised
    what production happens to have switched on would stop covering the rest.
    """
    try:
        return _load("live.yaml")
    except FileNotFoundError:
        return {}


def enabled_agents() -> list[str]:
    configured = live_config().get("enabled_agents")
    return list(configured) if configured else list(agents_config()["agents"])


def world_seed() -> dict[str, Any]:
    return _fresh("world_seed.yaml")


def tool_catalog() -> dict[str, Any]:
    return _fresh("tool_catalog.yaml")


def reload_all() -> None:
    """Clear the cache. Called after the self-healing loop edits prompts or config."""
    _load.cache_clear()


def dig(data: dict[str, Any], path: str, default: Any = None) -> Any:
    """Read by dotted path: dig(rules, "pricing.emergency_deposit.amount")."""
    node: Any = data
    for part in path.split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
                continue
            except (ValueError, IndexError):
                return default
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node

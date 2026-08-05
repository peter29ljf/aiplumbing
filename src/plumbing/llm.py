"""DeepSeek client wrapper (OpenAI-compatible API).

Every role — business agents, the simulated customer, technician, supervisor, the judge
and doctor — goes through here. Model ids, temperatures and base_url all come from
config/llm.yaml; nothing is hard-coded.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from plumbing import config
from plumbing.paths import load_dotenv


def active_provider(cfg: dict[str, Any]) -> dict[str, Any]:
    """The provider block config selects with `active:`.

    Older configs carried a single flat `provider:` mapping; that still works, so a
    checkout mid-switch does not break.
    """
    providers = cfg.get("providers")
    if not providers:
        return cfg["provider"]
    name = cfg.get("active")
    if name not in providers:
        raise LLMError(
            f"config/llm.yaml selects active provider '{name}', which is not one of "
            f"{sorted(providers)}"
        )
    return providers[name]


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    """Token usage accumulated across a whole run, reported for cost visibility.

    Cache hits are tracked separately. Each agent's system prompt plus tool schemas form a
    fixed prefix, and conversations only ever append, so the hit rate should be high. A low
    rate means something is breaking the prefix and is worth investigating — cached input
    tokens typically bill at a fraction of uncached ones.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    calls: int = 0
    by_role: dict[str, int] = field(default_factory=dict)

    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def add(
        self,
        role: str,
        prompt: int,
        completion: int,
        cache_hit: int = 0,
        cache_miss: int = 0,
    ) -> None:
        with self._lock:
            self._add_locked(role, prompt, completion, cache_hit, cache_miss)

    def _add_locked(
        self,
        role: str,
        prompt: int,
        completion: int,
        cache_hit: int,
        cache_miss: int,
    ) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cache_hit_tokens += cache_hit
        self.cache_miss_tokens += cache_miss
        self.calls += 1
        self.by_role[role] = self.by_role.get(role, 0) + 1

    @property
    def cache_hit_rate(self) -> float:
        """Share of input tokens served from cache."""
        return round(self.cache_hit_tokens / self.prompt_tokens, 3) if self.prompt_tokens else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cache_hit_rate": self.cache_hit_rate,
            "by_role": dict(self.by_role),
        }


class LLM:
    """Call the provider per role. One instance per run, so usage accumulates."""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        load_dotenv()
        self.cfg = cfg or config.llm_config()
        provider = active_provider(self.cfg)
        self.provider_name = self.cfg.get("active", "provider")
        self._default_model = provider.get("model")

        key_env = provider.get("api_key_env", "DEEPSEEK_API_KEY")
        api_key = os.environ.get(key_env)
        if not api_key:
            raise LLMError(
                f"Environment variable {key_env} is not set. Copy .env.example to .env and "
                f"fill in the key for the provider named in config/llm.yaml."
            )

        # The override variable is named after the key variable, so swapping providers does
        # not leave a DEEPSEEK_BASE_URL quietly pointing the new one at the old endpoint.
        url_env = provider.get("base_url_env") or key_env.replace("_API_KEY", "_BASE_URL")
        base_url = os.environ.get(url_env) or provider["base_url"]
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=provider.get("timeout_seconds", 120),
            max_retries=0,  # retry here instead, so attempts can be logged
        )
        self._max_retries = provider.get("max_retries", 3)
        # Vendor-specific switches the OpenAI schema has no field for. Qwen needs
        # enable_thinking here: it reasons by default, and on this workload the thinking
        # was 97% of the output tokens for no gain in the answer.
        self._extra_body = dict(provider.get("extra_body") or {})
        self.usage = Usage()

    # ------------------------------------------------------------------
    def role_settings(self, role: str) -> dict[str, Any]:
        """Role settings with the active provider's model filled in.

        A role only names a model when it should differ from the rest — otherwise
        switching provider would mean editing every role and missing one.
        """
        roles = self.cfg.get("roles", {})
        if role not in roles:
            raise LLMError(f"No role '{role}' under roles in config/llm.yaml")
        settings = dict(roles[role])
        settings.setdefault("model", self._default_model)
        if not settings["model"]:
            raise LLMError(
                f"Role '{role}' has no model and provider "
                f"'{self.cfg.get('active')}' declares no default in config/llm.yaml"
            )
        return settings

    def limit(self, name: str, default: int) -> int:
        return int(self.cfg.get("limits", {}).get(name, default))

    # ------------------------------------------------------------------
    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        """Call chat completions and return the message. Retries with exponential backoff."""
        settings = self.role_settings(role)
        kwargs: dict[str, Any] = {
            "model": settings["model"],
            "messages": messages,
            "temperature": settings.get("temperature", 0.3),
            "max_tokens": settings.get("max_tokens", 2000),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if response_format:
            kwargs["response_format"] = response_format
        extra_body = {**self._extra_body, **(settings.get("extra_body") or {})}
        if extra_body:
            kwargs["extra_body"] = extra_body

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self.client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - back off on network and rate-limit errors
                last_error = exc
                if attempt >= self._max_retries or _is_fatal(exc):
                    break
                time.sleep(min(2**attempt + random.random(), 20))
                continue

            if response.usage:
                hit, miss = _cache_tokens(response.usage)
                self.usage.add(
                    role,
                    response.usage.prompt_tokens or 0,
                    response.usage.completion_tokens or 0,
                    hit,
                    miss,
                )
            return response.choices[0].message

        raise LLMError(
            f"Role '{role}' failed calling model '{settings['model']}': {last_error}\n"
            f"If the model does not exist, run `python3 scripts/check_llm.py` to list the real "
            f"available ids, then update config/llm.yaml."
        ) from last_error

    # ------------------------------------------------------------------
    def chat_text(self, role: str, messages: list[dict[str, Any]]) -> str:
        """Text-only convenience entry point, used by the human simulators and doctor."""
        message = self.chat(role, messages)
        return (message.content or "").strip()

    def chat_json(
        self, role: str, messages: list[dict[str, Any]], retries: int = 2
    ) -> dict[str, Any]:
        """Ask for a JSON object. On a parse failure, re-ask with the error attached."""
        convo = list(messages)
        for _ in range(retries + 1):
            text = self.chat_text_json_mode(role, convo)
            parsed = _extract_json(text)
            if parsed is not None:
                return parsed
            convo = convo + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": "That reply was not valid JSON. Output a single JSON object only, with no explanation and no code fences.",
                },
            ]
        raise LLMError(f"Role '{role}' repeatedly failed to return valid JSON")

    def chat_text_json_mode(self, role: str, messages: list[dict[str, Any]]) -> str:
        try:
            message = self.chat(
                role, messages, response_format={"type": "json_object"}
            )
        except LLMError:
            # Fall back to plain mode when the provider does not support json_object
            message = self.chat(role, messages)
        return (message.content or "").strip()


def _cache_tokens(usage: Any) -> tuple[int, int]:
    """Extract cache hit/miss token counts from a usage object.

    DeepSeek reports prompt_cache_hit_tokens / prompt_cache_miss_tokens; OpenAI reports
    prompt_tokens_details.cached_tokens. Both are accepted; anything else counts as no cache.
    """
    extra = getattr(usage, "model_extra", None) or {}

    def pick(name: str) -> int | None:
        value = getattr(usage, name, None)
        if value is None:
            value = extra.get(name)
        return int(value) if value is not None else None

    hit = pick("prompt_cache_hit_tokens")
    miss = pick("prompt_cache_miss_tokens")
    if hit is not None:
        return hit, miss if miss is not None else max((usage.prompt_tokens or 0) - hit, 0)

    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details else None
    if cached is None and isinstance(details, dict):
        cached = details.get("cached_tokens")
    if cached is not None:
        cached = int(cached)
        return cached, max((usage.prompt_tokens or 0) - cached, 0)

    return 0, 0


def _is_fatal(exc: Exception) -> bool:
    """Auth errors and missing models will never succeed on retry."""
    status = getattr(exc, "status_code", None)
    return status in (400, 401, 403, 404)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Tolerate code fences and surrounding chatter; extract the first JSON object."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(cleaned)):
        if cleaned[index] == "{":
            depth += 1
        elif cleaned[index] == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(cleaned[start : index + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None

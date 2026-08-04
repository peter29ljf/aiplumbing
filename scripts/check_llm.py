#!/usr/bin/env python3
"""DeepSeek connectivity probe.

Confirms the key works, lists the real available model ids, verifies that the configured
ids resolve, checks tool calling (the whole framework depends on it), and confirms that
context caching is actually taking effect.

    python3 scripts/check_llm.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing import config  # noqa: E402
from plumbing.llm import LLM, LLMError  # noqa: E402


def main() -> int:
    try:
        llm = LLM()
    except LLMError as exc:
        print(f"[x] {exc}")
        return 1

    cfg = config.llm_config()
    print(f"[i] base_url: {llm.client.base_url}")

    # --- 1. List available models ---------------------------------------
    available: set[str] = set()
    print("\n=== Available model ids ===")
    try:
        for model in llm.client.models.list().data:
            available.add(model.id)
            print(f"  - {model.id}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] /v1/models unavailable (not fatal): {exc}")

    # --- 2. Check the configured models ---------------------------------
    configured = {role: spec["model"] for role, spec in cfg["roles"].items()}
    print("\n=== Models configured in config/llm.yaml ===")
    for role, model in configured.items():
        mark = "?" if not available else ("ok" if model in available else "MISSING")
        print(f"  [{mark}] {role:<12} -> {model}")
    if available:
        missing = {m for m in configured.values() if m not in available}
        if missing:
            print(f"\n[!] These model ids are not in the available list: {sorted(missing)}")
            print("    Update roles.*.model in config/llm.yaml to one of the ids above.")

    # --- 2b. Doctor backend ---------------------------------------------
    backend = cfg.get("doctor_backend", {}) or {}
    print(f"\n=== Doctor backend ===\n  kind: {backend.get('kind', 'claude_cli')}")
    if backend.get("kind", "claude_cli") == "claude_cli":
        import shutil
        import subprocess

        cli = shutil.which("claude")
        if not cli:
            print("  [!] `claude` CLI not found on PATH. Either install the Claude Code CLI "
                  "or set doctor_backend.kind to 'openai' in config/llm.yaml.")
        else:
            try:
                version = subprocess.run(
                    ["claude", "--version"], capture_output=True, text=True, timeout=30
                ).stdout.strip()
            except Exception as exc:  # noqa: BLE001
                version = f"(version check failed: {exc})"
            print(f"  [ok] {cli} — {version}")
            print(f"  model: {backend.get('model', 'claude-opus-5')}")

    # --- 3. Live call, verifying tool calling -----------------------------
    print("\n=== Live call test (agent role) ===")
    probe_tool = [
        {
            "type": "function",
            "function": {
                "name": "get_price",
                "description": "Look up the price of a service",
                "parameters": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
            },
        }
    ]
    try:
        message = llm.chat(
            "agent",
            [
                {
                    "role": "system",
                    "content": "You are a test assistant. Always call the tool for prices; "
                    "never invent one.",
                },
                {"role": "user", "content": "How much is a standard call-out?"},
            ],
            tools=probe_tool,
        )
    except LLMError as exc:
        print(f"[x] Call failed: {exc}")
        return 1

    if getattr(message, "tool_calls", None):
        call = message.tool_calls[0]
        print(f"[ok] tool calling works -> {call.function.name}({call.function.arguments})")
    else:
        print(f"[!] The model replied with text instead of calling the tool: "
              f"{(message.content or '')[:120]}")
        print("    Warning: this framework depends on tool calling. Confirm the model "
              "supports function calling.")

    # --- 4. Context cache probe -------------------------------------------
    # Send the same long prefix twice; the second should hit cache. A zero hit rate means
    # caching is not in effect, which makes a full suite run considerably more expensive.
    print("\n=== Context cache probe ===")
    filler = "Company policy reference material. " * 300  # long enough to be cacheable
    probe_messages = [
        {"role": "system", "content": f"You are a plumbing support agent. Background: {filler}"},
        {"role": "user", "content": "Reply with exactly the word: OK"},
    ]
    before = llm.usage.cache_hit_tokens
    llm.chat("agent", probe_messages)
    first_hit = llm.usage.cache_hit_tokens - before

    before = llm.usage.cache_hit_tokens
    llm.chat("agent", probe_messages)
    second_hit = llm.usage.cache_hit_tokens - before

    print(f"  First request with this prefix, cache hit: {first_hit} tokens")
    print(f"  Second request with this prefix, cache hit: {second_hit} tokens")
    if second_hit > 0:
        print("[ok] Context caching is working; repeated prefixes are reused.")
    else:
        print("[!] No cache hits observed. This model or account may not support context")
        print("    caching, or usage does not report the fields. Functionality is unaffected,")
        print("    but runs will cost more.")

    print(f"\n[i] Usage for this probe: {llm.usage.as_dict()}")
    print("[ok] Probe complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

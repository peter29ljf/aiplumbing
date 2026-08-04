"""Self-healing: read a failing scenario, rewrite the agent prompt.

**Anti-cheating is the point of this module.** The doctor may only change Markdown
prompts under `agents/`:

- not Python code (that would bypass the tool-layer hard gates)
- not `config/business_rules.yaml` (rewriting the rules to pass a test is tampering)
- not `scenarios/` (rewriting the exam to pass it)

Two backends, selected by `doctor_backend.kind` in config/llm.yaml:

- `claude_cli`  — runs the Claude Code CLI with permission checks bypassed, letting a
  strong model read the repo and edit the prompt files directly. Guards are enforced
  *after the fact*: every protected file is hashed beforehand and restored if touched.
- `openai`      — one-shot JSON patch from the configured OpenAI-compatible model.

Either way the result is a Patch that can be applied and reverted, so a change that
fixes one scenario but breaks another is always recoverable.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from plumbing import config
from plumbing.llm import LLM, LLMError
from plumbing.paths import AGENTS_DIR, PROMPT_HISTORY_DIR, ROOT

# Directories the doctor must never modify, whatever backend is used.
PROTECTED_DIRS = ["config", "scenarios", "src", "tests", "scripts"]
PROTECTED_FILES = ["README.md", "pytest.ini"]


# ======================================================================
# Patch
# ======================================================================


@dataclass
class Patch:
    """One or more prompt files changed together, with the means to undo them."""

    changes: dict[str, tuple[str, str]]   # relative path under agents/ -> (old, new)
    reason: str
    scenario_id: str
    backend: str = ""
    history_path: Path | None = None

    @property
    def files(self) -> list[str]:
        return sorted(self.changes)

    @property
    def file(self) -> str:
        """Primary file, for reporting."""
        return self.files[0] if self.changes else "-"


def apply(patch: Patch) -> Patch:
    """Write the change to disk and record a history snapshot.

    For the CLI backend the files are already written; this still records history
    and normalises state so revert() works identically for both backends.
    """
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = patch.file.replace("/", "_").replace(".md", "")
    history = PROMPT_HISTORY_DIR / f"{stamp}-{slug}-{patch.scenario_id}.md"

    sections = [
        "# Prompt change record",
        "",
        f"- Time: {datetime.now().isoformat()}",
        f"- Backend: {patch.backend}",
        f"- Triggering scenario: {patch.scenario_id}",
        f"- Files: {', '.join('agents/' + f for f in patch.files)}",
        f"- Reason: {patch.reason}",
        "",
    ]
    for rel, (old, new) in sorted(patch.changes.items()):
        (AGENTS_DIR / rel).write_text(new, encoding="utf-8")
        sections += [
            f"## agents/{rel} — before",
            "",
            "```markdown",
            old,
            "```",
            "",
            f"## agents/{rel} — after",
            "",
            "```markdown",
            new,
            "```",
            "",
        ]

    history.write_text("\n".join(sections), encoding="utf-8")
    patch.history_path = history
    config.reload_all()
    return patch


def revert(patch: Patch) -> None:
    for rel, (old, _new) in patch.changes.items():
        (AGENTS_DIR / rel).write_text(old, encoding="utf-8")
    config.reload_all()
    if patch.history_path and patch.history_path.exists():
        patch.history_path.write_text(
            patch.history_path.read_text(encoding="utf-8")
            + "\n\n> **This change was reverted** — it did not fix the scenario, or it "
            "broke another scenario in regression.\n",
            encoding="utf-8",
        )


# ======================================================================
# Shared context building
# ======================================================================


def editable_files(agents_involved: list[str]) -> dict[str, str]:
    """Files the doctor may edit: the agents' own prompts plus their shared fragments."""
    cfg = config.agents_config()
    files: dict[str, str] = {}
    for name in agents_involved or list(cfg["agents"]):
        spec = cfg["agents"].get(name)
        if not spec:
            continue
        # Stub prompts are placeholders; editing them proves nothing.
        if spec.get("stub"):
            continue
        files[spec["prompt"]] = (AGENTS_DIR / spec["prompt"]).read_text(encoding="utf-8")
        for shared in spec.get("shared", []):
            rel = f"_shared/{shared}.md"
            files[rel] = (AGENTS_DIR / rel).read_text(encoding="utf-8")
    return files


GUIDANCE = """\
# How to fix it

1. **Change one file if you can.** Pick the one that actually addresses the root cause.
2. **Make the smallest change that works.** Keep the existing structure and wording;
   add or remove only what is needed. Do not rewrite the whole file.
3. **Fix the cause, not the symptom.** Never write a scenario-specific instruction like
   "in this case say X". Write a generalisable rule, or you will break other scenarios.
4. **Prefer the shared fragment** (`_shared/*.md`) when the problem affects every agent;
   edit an individual agent's own file when it is specific to that agent.
5. If the agent failed to call a tool, state clearly in the prompt *when that tool must
   be called* — do not just say "use more tools".
"""


def _failure_context(result: Any, scenario: dict[str, Any]) -> str:
    failures = "\n".join(f"- [{f['name']}] {f['detail']}" for f in result.failures)
    return f"""\
# The failing test

- id: {result.scenario_id}
- description: {result.description}
- agents involved: {', '.join(result.agents_involved)}
- conversation ended by: {result.ended_by} ({result.end_reason})

# What failed

{failures}

# Scenario expectations (the standard — you may NOT change these)

```yaml
{_dump_yaml(scenario.get('expect', {}))}
```

# Simulated customer definition (you may NOT change this)

```yaml
{_dump_yaml(scenario.get('customer', {}))}
```

# Actual conversation

{result.transcript_text}

# Tool call log

{_format_tools(result.tool_log)}
"""


# ======================================================================
# Entry point
# ======================================================================


def propose(
    llm: LLM,
    result: Any,
    scenario: dict[str, Any],
    previous_attempts: list[str] | None = None,
) -> Patch | None:
    backend = config.llm_config().get("doctor_backend", {}) or {}
    kind = backend.get("kind", "claude_cli")
    if kind == "claude_cli":
        return _propose_via_claude_cli(result, scenario, previous_attempts, backend)
    return _propose_via_llm(llm, result, scenario, previous_attempts)


# ======================================================================
# Backend: Claude Code CLI
# ======================================================================


def _hash_tree() -> dict[str, str]:
    """Hash every protected file so unauthorised edits can be detected and undone."""
    digest: dict[str, str] = {}
    targets: list[Path] = []
    for name in PROTECTED_DIRS:
        directory = ROOT / name
        if directory.exists():
            targets.extend(p for p in directory.rglob("*") if p.is_file())
    targets.extend(ROOT / name for name in PROTECTED_FILES if (ROOT / name).exists())
    for path in targets:
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        try:
            digest[str(path.relative_to(ROOT))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        except OSError:
            continue
    return digest


def _propose_via_claude_cli(
    result: Any,
    scenario: dict[str, Any],
    previous_attempts: list[str] | None,
    backend: dict[str, Any],
) -> Patch | None:
    model = backend.get("model", "claude-opus-5")
    timeout = int(backend.get("timeout_seconds", 900))

    editable = editable_files(result.agents_involved)
    if not editable:
        return None

    before_prompts = {
        rel: (AGENTS_DIR / rel).read_text(encoding="utf-8")
        for rel in _all_prompt_files()
    }
    protected_before = _hash_tree()

    attempts_note = ""
    if previous_attempts:
        attempts_note = (
            "\n# Fixes already tried and rejected\n\n"
            + "\n".join(f"- {a}" for a in previous_attempts)
            + "\n\nTake a different approach. Do not repeat what has already failed.\n"
        )

    task = f"""\
You are a prompt engineer for an AI agent system. A plumbing company's customer-service
agents failed an automated test. Fix the **agent prompt files** so the test passes next
time, without breaking the scenarios that already pass.

Working directory: {ROOT}

# Hard constraints

You may ONLY edit Markdown files under `agents/`. Specifically these:

{chr(10).join('- agents/' + f for f in sorted(editable))}

You must NOT edit anything else. In particular you must not touch:
- any Python source under `src/` or `tests/`
- `config/business_rules.yaml` or any other file under `config/`
- anything under `scenarios/`

Those files are hashed before and after you run. If you modify any of them, your entire
change is discarded automatically and this repair attempt is recorded as a failure.
Rewriting the rules or the tests to make a test pass is cheating, not fixing.

You may freely read any file in the repository to understand the system.

{_failure_context(result, scenario)}
{attempts_note}
{GUIDANCE}

# What to do

Read whatever you need, then edit the prompt file(s) with your fix.

When you are done, print a final line in exactly this form and nothing after it:

REASON: <one or two sentences explaining the root cause and what you changed>
"""

    command = [
        "claude",
        "-p",
        task,
        "--model",
        model,
        "--dangerously-skip-permissions",
        # The doctor edits prompts; it has no business running shell commands.
        "--disallowedTools",
        "Bash",
    ]
    command.extend(backend.get("extra_args", []) or [])

    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        print("    [doctor] `claude` CLI not found on PATH. Set doctor_backend.kind to "
              "'openai' in config/llm.yaml, or install the Claude Code CLI.")
        return None
    except subprocess.TimeoutExpired:
        print(f"    [doctor] Claude CLI timed out after {timeout}s")
        _restore_prompts(before_prompts)
        return None

    if completed.returncode != 0:
        print(f"    [doctor] Claude CLI exited {completed.returncode}: "
              f"{(completed.stderr or '')[:300]}")
        _restore_prompts(before_prompts)
        return None

    # --- Guard: did it touch anything it was told not to? ---------------
    protected_after = _hash_tree()
    tampered = [
        path
        for path, digest in protected_after.items()
        if protected_before.get(path) != digest
    ] + [path for path in protected_before if path not in protected_after]
    if tampered:
        print(f"    [doctor] REJECTED — modified protected files: {sorted(tampered)[:5]}")
        print("    [doctor] Reverting prompt changes as well; this attempt is discarded.")
        _restore_prompts(before_prompts)
        return None

    # --- Collect what actually changed ----------------------------------
    changes: dict[str, tuple[str, str]] = {}
    for rel, old in before_prompts.items():
        new = (AGENTS_DIR / rel).read_text(encoding="utf-8")
        if new != old:
            changes[rel] = (old, new)

    if not changes:
        print("    [doctor] Claude CLI made no changes to any prompt file")
        return None

    disallowed = [rel for rel in changes if rel not in editable]
    if disallowed:
        print(f"    [doctor] REJECTED — edited prompts outside the allowed set: {disallowed}")
        _restore_prompts(before_prompts)
        return None

    for rel, (old, new) in changes.items():
        if len(new) < len(old) * 0.5:
            print(f"    [doctor] REJECTED — agents/{rel} shrank from {len(old)} to "
                  f"{len(new)} chars, looks truncated")
            _restore_prompts(before_prompts)
            return None

    reason = _extract_reason(completed.stdout) or "(no reason given)"
    return Patch(
        changes=changes,
        reason=reason,
        scenario_id=result.scenario_id,
        backend=f"claude_cli:{model}",
    )


def _all_prompt_files() -> list[str]:
    files = [f"_shared/{p.name}" for p in sorted((AGENTS_DIR / "_shared").glob("*.md"))]
    files += [p.name for p in sorted(AGENTS_DIR.glob("*.md"))]
    return files


def _restore_prompts(snapshot: dict[str, str]) -> None:
    for rel, content in snapshot.items():
        path = AGENTS_DIR / rel
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
    config.reload_all()


def _extract_reason(stdout: str) -> str:
    for line in reversed((stdout or "").splitlines()):
        stripped = line.strip()
        if stripped.upper().startswith("REASON:"):
            return stripped[7:].strip()
    tail = (stdout or "").strip().splitlines()
    return tail[-1].strip()[:300] if tail else ""


# ======================================================================
# Backend: OpenAI-compatible one-shot JSON patch
# ======================================================================

_SYSTEM = f"""You are a prompt engineer for AI agents. A plumbing company's customer service
agents failed an automated test. Your job is to **edit the agent prompt files** so the test
passes next time, without breaking scenarios that already pass.

# You may only edit prompts

The only thing you can change is the Markdown prompt files listed below. You **cannot**
change code, the business rules table, or the test scenarios. If you believe the failure
is caused by a misconfigured rule or a badly written test, say so in `reason` and make the
smallest useful prompt change anyway — do not try to work around the guard.

{GUIDANCE}

# Output format

Output a single JSON object, no code fences:

{{
  "file": "intake.md",
  "reason": "why you changed it, two or three sentences naming the root cause",
  "new_content": "the complete file content after your change"
}}

`file` must be one of the relative paths in the file list.
`new_content` is the **complete** content of that file after the change — not a diff,
with nothing omitted.
"""


def _propose_via_llm(
    llm: LLM,
    result: Any,
    scenario: dict[str, Any],
    previous_attempts: list[str] | None = None,
) -> Patch | None:
    files = editable_files(result.agents_involved)
    if not files:
        return None

    file_blocks = "\n\n".join(
        f"## File: {name}\n```markdown\n{content}\n```" for name, content in files.items()
    )
    attempts_note = ""
    if previous_attempts:
        attempts_note = (
            "\n# Fixes already tried and rejected\n\n"
            + "\n".join(f"- {a}" for a in previous_attempts)
            + "\n\nTake a different approach.\n"
        )

    payload = (
        f"{_failure_context(result, scenario)}\n{attempts_note}\n"
        f"# Editable prompt files\n\n{file_blocks}\n\nMake your change."
    )

    try:
        reply = llm.chat_json(
            "doctor",
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": payload},
            ],
        )
    except LLMError as exc:
        print(f"    [doctor] model call failed: {exc}")
        return None

    target = str(reply.get("file", "")).strip()
    new_content = str(reply.get("new_content", ""))
    reason = str(reply.get("reason", "")).strip()

    if target not in files:
        print(f"    [doctor] rejected: '{target}' is not an editable file {sorted(files)}")
        return None
    if not new_content.strip():
        print("    [doctor] rejected: new_content is empty")
        return None
    if new_content.strip() == files[target].strip():
        print("    [doctor] rejected: content unchanged")
        return None
    if len(new_content) < len(files[target]) * 0.5:
        print(
            f"    [doctor] rejected: new content ({len(new_content)} chars) is far shorter "
            f"than the original ({len(files[target])}), looks truncated"
        )
        return None

    return Patch(
        changes={target: (files[target], new_content)},
        reason=reason,
        scenario_id=result.scenario_id,
        backend="openai",
    )


# ======================================================================
# Helpers
# ======================================================================


def _dump_yaml(data: Any) -> str:
    import yaml

    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()


def _format_tools(tool_log: list[dict[str, Any]]) -> str:
    import json

    lines = []
    for index, entry in enumerate(tool_log, 1):
        status = "OK" if entry.get("ok") else "FAILED"
        args = json.dumps(entry.get("arguments", {}), ensure_ascii=False)[:200]
        tail = (
            json.dumps(entry.get("result", {}), ensure_ascii=False, default=str)[:200]
            if entry.get("ok")
            else str(entry.get("error", ""))[:200]
        )
        lines.append(f"{index}. [{status}] {entry['tool']} args={args}\n     -> {tail}")
    return "\n".join(lines) or "(no tool calls)"

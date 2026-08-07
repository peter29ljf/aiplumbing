"""Drive Claude Code from outside, and keep the receipt.

The generator does not write files itself. It hands the job to `claude -p` running headless
in the project's own directory, watches what comes back, and records what it cost. That is
a deliberate division: writing a rules file well is the thing Claude Code is good at, and
knowing whether the resulting agent works is the thing the harness is good at.

**Confined on purpose.** `--dangerously-skip-permissions` is what makes an unattended run
possible at all, and it is only safe because of the two flags next to it: `--add-dir` gives
one project directory and nothing else, and `--allowedTools` gives a Bash that can run the
harness and nothing else. An unattended session does not need a general shell, and the day
it seems to is the day to stop and look at why.

Field names here were read off a real invocation, not remembered — see `probe()`.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# What the builder may do inside a project. Bash is a single command, not a shell: the
# harness is the only thing it needs to run, and everything else it needs is a file tool.
ALLOWED_TOOLS = (
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(python3 -m bat.runtime.harness:*)",
)


class ClaudeFailed(RuntimeError):
    """The CLI exited badly, or said it errored. Carries whatever it managed to say."""


# The one failure where retrying is pointless and the fix is outside the system. Worth
# telling apart from every other error: three builds stopped here mid-way and the console
# said "failed", which reads like something to debug rather than something to top up.
OUT_OF_CREDIT = "Credit balance is too low"


def is_out_of_credit(error: str) -> bool:
    return OUT_OF_CREDIT.lower() in (error or "").lower()


@dataclass
class Spend:
    """What one invocation cost. Kept per call, not per session, because a session that
    got expensive did so somewhere, and an aggregate cannot say where."""

    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def cache_hit_rate(self) -> float:
        """Of everything the model was shown, how much it had already seen.

        The number worth watching. On the agent side, the same model on a different
        endpoint went from 60% to 84% and halved the input bill without a prompt changing.
        """
        shown = self.input_tokens + self.cache_read + self.cache_write
        return round(self.cache_read / shown, 3) if shown else 0.0

    def __add__(self, other: "Spend") -> "Spend":
        return Spend(
            usd=self.usd + other.usd,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "usd": round(self.usd, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "cache_hit_rate": self.cache_hit_rate,
        }


@dataclass
class Reply:
    text: str = ""
    session_id: str = ""
    spend: Spend = field(default_factory=Spend)
    turns: int = 0
    seconds: float = 0.0
    error: str = ""
    denials: list[Any] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


def spend_of(usage: dict[str, Any], usd: float) -> Spend:
    """Read the CLI's usage block. Names taken from a real result, not from memory."""
    return Spend(
        usd=float(usd or 0.0),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
        cache_write=int(usage.get("cache_creation_input_tokens") or 0),
    )


def command(prompt: str, *, project_dir: Path, session_id: str = "",
            model: str = "", system: str = "",
            extra_dirs: tuple[Path, ...] = ()) -> list[str]:
    """The argv. Built here so a test can read it without running anything, and so the
    confinement flags are in one place where they can be audited."""
    argv = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--add-dir", str(project_dir),
        "--allowedTools", *ALLOWED_TOOLS,
    ]
    # The standing half goes here rather than into the prompt. Same reason as the other
    # backend: a system prompt is a stable prefix the provider can cache, and five
    # thousand characters re-sent as fresh prompt text every turn is not.
    if system:
        argv += ["--append-system-prompt", system]
    for extra in extra_dirs:
        argv += ["--add-dir", str(extra)]
    if model:
        argv += ["--model", model]
    if session_id:
        argv += ["--resume", session_id]
    return argv


def stream(prompt: str, *, project_dir: Path, session_id: str = "", model: str = "",
           system: str = "", extra_dirs: tuple[Path, ...] = (),
           on_event: Callable[[dict[str, Any]], None] | None = None,
           stop: threading.Event | None = None,
           timeout: float = 3600.0) -> Iterator[dict[str, Any]]:
    """Run it, yielding each event as it arrives.

    Streaming rather than waiting, because a build takes minutes and a person watching a
    blank page cannot tell it apart from a hang. The console forwards these straight to
    the browser.

    `stop` is how a pause button works: checked between events, so the run ends after the
    current message rather than in the middle of a file write.
    """
    argv = command(prompt, project_dir=project_dir, session_id=session_id, model=model,
                   system=system, extra_dirs=extra_dirs)
    env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "bat-builder"}

    process = subprocess.Popen(
        argv, cwd=str(project_dir), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    assert process.stdout is not None

    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Not every line is ours — a warning on stdout should not end the build.
                continue
            if on_event is not None:
                on_event(event)
            yield event
            if stop is not None and stop.is_set():
                process.terminate()
                yield {"type": "bat.stopped", "reason": "asked to stop"}
                return
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        leftovers = (process.stderr.read() if process.stderr else "") or ""
        if process.returncode not in (0, None) and leftovers.strip():
            if on_event is not None:
                on_event({"type": "bat.stderr", "text": leftovers[-2000:]})


def run(prompt: str, *, project_dir: Path, session_id: str = "", model: str = "",
        system: str = "", extra_dirs: tuple[Path, ...] = (),
        on_event: Callable[[dict[str, Any]], None] | None = None,
        stop: threading.Event | None = None,
        ledger: Path | None = None, phase: str = "") -> Reply:
    """Drain the stream and hand back the one thing a caller usually wants.

    `ledger` is written the moment the result arrives rather than after this returns. A
    ten-minute build killed by an impatient timeout has already been billed by Anthropic;
    recording it only on a clean return meant the money was spent and the ledger said
    nothing, which is the one thing a ledger must never do.
    """
    reply = Reply(session_id=session_id)
    for event in stream(prompt, project_dir=project_dir, session_id=session_id,
                        model=model, system=system, extra_dirs=extra_dirs,
                        on_event=on_event, stop=stop):
        reply = absorb(reply, event)
        if event.get("type") == "result" and ledger is not None:
            record(reply, ledger, phase=phase, prompt=prompt)
    return reply


def absorb(reply: Reply, event: dict[str, Any]) -> Reply:
    """Fold one event into the reply. Separate from `run` so the console can keep a
    running total for a stream it is forwarding rather than draining."""
    kind = event.get("type")

    if kind == "system" and event.get("session_id"):
        reply.session_id = str(event["session_id"])

    elif kind == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text":
                reply.text += block.get("text") or ""

    elif kind == "result":
        reply.session_id = str(event.get("session_id") or reply.session_id)
        reply.spend = reply.spend + spend_of(event.get("usage") or {},
                                             event.get("total_cost_usd") or 0.0)
        reply.turns = int(event.get("num_turns") or 0)
        reply.seconds = round(float(event.get("duration_ms") or 0) / 1000, 1)
        reply.denials = list(event.get("permission_denials") or [])
        # `result` is the final text. Prefer it: the assistant blocks include thinking
        # aloud on the way, and the last word is the answer.
        if isinstance(event.get("result"), str) and event["result"].strip():
            reply.text = event["result"]
        if event.get("is_error") or event.get("subtype") not in (None, "success"):
            reply.error = str(event.get("result") or event.get("subtype") or "failed")

    elif kind == "bat.stderr":
        reply.error = reply.error or str(event.get("text") or "")[:2000]

    elif kind == "bat.stopped":
        reply.error = reply.error or "stopped"

    return reply


def record(reply: Reply, ledger: Path, *, phase: str, prompt: str) -> None:
    """Append one line to the project's spend ledger.

    Two meters are kept apart deliberately: this one is Anthropic's, for building the
    agent. What the finished agent costs to *run* is its provider's, and lives in the
    harness reports. Adding them together would hide which one is worth attacking.
    """
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as out:
        out.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "phase": phase,
            "session_id": reply.session_id,
            "turns": reply.turns,
            "seconds": reply.seconds,
            "error": reply.error,
            "asked": prompt[:400],
            **reply.spend.as_dict(),
            # Full precision, overriding the rounded one `as_dict` produces for display.
            # A ledger of rounded lines does not add up to the rounded total, and a
            # billing figure that disagrees with itself is worse than no figure.
            "usd": reply.spend.usd,
        }) + "\n")


def total(ledger: Path) -> tuple[Spend, int]:
    """Everything spent building this project, and how many calls it took."""
    if not ledger.exists():
        return Spend(), 0
    running, calls = Spend(), 0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        running = running + Spend(
            usd=float(row.get("usd") or 0),
            input_tokens=int(row.get("input_tokens") or 0),
            output_tokens=int(row.get("output_tokens") or 0),
            cache_read=int(row.get("cache_read") or 0),
            cache_write=int(row.get("cache_write") or 0),
        )
        calls += 1
    return running, calls


def probe() -> dict[str, Any]:
    """One cheap call, to read the result shape rather than trust a memory of it.

    Kept because it has already earned itself once: the fields are `total_cost_usd` and
    `usage.cache_read_input_tokens`, neither of which is what anybody guesses.
    """
    out = subprocess.run(
        ["claude", "-p", "Reply with exactly: ok", "--output-format", "json",
         "--model", "haiku"],
        capture_output=True, text=True, timeout=180,
    )
    return json.loads(out.stdout)

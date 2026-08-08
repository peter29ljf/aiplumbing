"""One node = one resident `claude -p` process, spoken to over stdin.

`--input-format stream-json` is the mechanism the whole idea rests on: the process does
not exit after answering. You write a line of JSON to its stdin per turn and read events
off its stdout, so process start and the MCP handshake are paid **once per node** rather
than once per turn.

The node boundary is still a process boundary, and deliberately so — `--system-prompt`
is fixed at launch, and dropping the message history at a node change is the compaction
the architecture is built on. What changes is the arithmetic: a conversation of twenty-five
turns across ten nodes costs ten process starts, not twenty-five.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent

# What the CLI says instead of answering, when there is no money left.
_A_WALL = re.compile(r"spend limit|usage limit|credit balance is too low|"
                     r"rate limit.*try again|not logged in", re.IGNORECASE)


class OutOfCredit(RuntimeError):
    """The account, not the agent. Nothing downstream of this is a measurement."""


@dataclass
class Turn:
    said: str
    seconds: float
    tools: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    aside: list[str] = field(default_factory=list)   # said between tool calls, not sent
    tokens_in: int = 0       # what the model was shown this turn
    cached: int = 0          # of which the provider already had
    cost: float = 0.0        # this turn alone
    running: float = 0.0     # the session total the result event reports
    events: int = 0


class Session:
    """A node's conversation. Enter it, say things to it, leave."""

    # Named rather than left to whatever the CLI defaults to that week. A suite's score
    # is a statement about a model, and a score whose model can change underneath it is a
    # statement about nothing.
    MODEL = "claude-opus-5"

    def __init__(self, *, node: str, system: str, allowed: list[str], exits: list[str],
                 world: Path, model: str = "", timeout: float = 420.0) -> None:
        # Generous, because the failure it prevents is indistinguishable from the
        # agent being broken: a scenario timed out with no model call made at all,
        # eighteen sessions having started in the same second.
        self.node, self.world, self.timeout = node, world, timeout
        self.allowed, self.exits = allowed, exits
        self.system = system
        self.model = model or self.MODEL
        # Set by a caller that wants a different MCP server than the probe's toy
        # one: (path, environment). The bridge points this at its proxy.
        # Where the process runs. Skills are discovered from `.claude/skills`
        # under it, so the skill engine points this at its own scratch dir.
        self.builtins = ""
        self.cwd: Path | None = None
        self.proxy: tuple[str, dict[str, str]] | None = None
        self.proc: subprocess.Popen | None = None
        self.began = 0.0
        self.startup = 0.0
        self.offered: list[str] = []
        self.turns: list[Turn] = []

    # -- the command ------------------------------------------------------

    def command(self) -> list[str]:
        script, extra = self.proxy or (str(HERE / "flow_mcp.py"), {
            "FLOW_WORLD": str(self.world),
            "FLOW_EXITS": ",".join(self.exits),
            # The real subset filter. See flow_mcp.py.
            "FLOW_TOOLS": ",".join(self.allowed),
        })
        mcp_config = json.dumps({"mcpServers": {"flow": {
            "command": sys.executable,
            "args": [script],
            "env": {**os.environ, **extra},
        }}})
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            # Replaces the coding-assistant prompt outright. `--append-system-prompt`
            # would leave it in place, and a plumber's booking step would still be
            # holding a set of file-editing instructions it must ignore.
            "--system-prompt", self.system,
            "--mcp-config", mcp_config,
            # Nothing but the flow tools exists. No Read, no Bash, no project MCP
            # servers leaking in from the user's own configuration.
            "--strict-mcp-config",
            # Two different flags, and the difference cost a run to find. `--allowedTools`
            # is a *permission* list — it says what may be used without asking, not what
            # exists. With it alone the built-in kit was still there, and the first real
            # turn spent a round trip on `ToolSearch` looking for the flow tools because
            # they had been deferred behind the rest. `--tools ""` is the availability
            # filter: nothing from the built-in set exists at all.
            # Empty unless a caller needs the built-ins back. The skill
            # engine does: a SKILL.md is a file, and reading it is Read.
            "--tools", self.builtins,
            "--allowedTools", ",".join(f"mcp__flow__{t}" for t in self.allowed),
            "--dangerously-skip-permissions",
            # No `--bare`. It looked right — skip hooks, LSP, plugin sync, CLAUDE.md, all
            # of which a booking step has no use for — but it also skips whatever
            # establishes credentials, and every turn came back "Not logged in · Please
            # run /login" while a plain `claude -p` answered fine. The CLAUDE.md half is
            # handled by running somewhere without one; see `cwd` below.
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    # -- lifetime ---------------------------------------------------------

    def __enter__(self) -> "Session":
        self.began = time.monotonic()
        self.proc = subprocess.Popen(
            self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            cwd=str(self.cwd) if self.cwd else None,
            env={**os.environ, "FLOW_WORLD": str(self.world)},
        )
        # Nothing is read here. The init event does not necessarily arrive before the
        # first message goes in, and waiting for it first deadlocks: the probe sat for
        # three minutes with an empty log and a live process, each side waiting for the
        # other to speak. Startup is measured off the init event whenever it shows up.
        return self

    def __exit__(self, *_: object) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()          # type: ignore[union-attr]
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()

    # -- speaking ---------------------------------------------------------

    def say(self, text: str) -> Turn:
        assert self.proc and self.proc.stdin
        began = time.monotonic()
        self.proc.stdin.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }) + "\n")
        self.proc.stdin.flush()

        turn = Turn(said="", seconds=0.0)
        said: list[str] = []
        after_a_refusal: set[int] = set()

        def watch(event: dict) -> bool:
            turn.events += 1
            if event.get("type") == "system" and not self.offered:
                # The handshake is done by the time this lands, so it dates the fixed
                # cost — process start plus MCP negotiation — and lists what the model
                # can actually see.
                self.startup = time.monotonic() - self.began
                self.offered = sorted(event.get("tools", []))
            elif event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        said.append(block["text"])
                    elif block.get("type") == "tool_use":
                        turn.tools.append(block.get("name", "?"))
            elif event.get("type") == "user":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result" and block.get("is_error"):
                        turn.denied.append(str(block.get("content"))[:200])
                        # What the model says straight after a refusal is it working out
                        # what went wrong, and it is addressed to nobody: "The send was
                        # refused — the details are on the ticket as notes but not in the
                        # fields the handover reads. Let me write them properly and try
                        # again." That went to a customer.
                        #
                        # Only that. Dropping *every* block before a tool call, which is
                        # what this did first, threw away the three appointment times a
                        # step had just written out before calling `step.finished` — so
                        # the customer was asked which of three times suited and shown
                        # none of them, and two scenarios failed for a fee that had been
                        # quoted into a block nobody kept.
                        after_a_refusal.add(len(said))
            return event.get("type") == "result"

        done = self._until(watch, label="result")
        # A wall, not an answer. When the account runs out, every turn comes back as this
        # one sentence and the suite reads it as the agent's reply: eighteen scenarios
        # "went round in circles", each with its own invented verdict about a step that
        # was never offered `step.finished`. A whole run's diagnosis, and not one word of
        # it was true. Better to stop and say what happened.
        if _A_WALL.search("\n".join(said)):
            raise OutOfCredit("\n".join(said).strip()[:200])
        turn.seconds = time.monotonic() - began
        turn.aside = [text for i, text in enumerate(said) if i in after_a_refusal]
        turn.said = "\n".join(text for i, text in enumerate(said)
                              if i not in after_a_refusal).strip()
        # What the model was actually shown. The whole question between the two
        # engines is whether a conversation's context grows as it walks the graph, and
        # this is the only number that answers it rather than arguing about it.
        usage = (done or {}).get("usage") or {}
        turn.tokens_in = int(usage.get("input_tokens") or 0) + \
            int(usage.get("cache_read_input_tokens") or 0) + \
            int(usage.get("cache_creation_input_tokens") or 0)
        turn.cached = int(usage.get("cache_read_input_tokens") or 0)
        turn.running = float((done or {}).get("total_cost_usd") or 0.0)
        before = self.turns[-1].running if self.turns else 0.0
        turn.cost = round(max(turn.running - before, 0.0), 6)
        self.turns.append(turn)
        return turn

    # -- reading events ---------------------------------------------------

    def _until(self, stop, *, label: str) -> dict | None:
        """Read events until `stop` says this one ends the wait."""
        assert self.proc and self.proc.stdout
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                err = (self.proc.stderr.read() or "").strip()  # type: ignore[union-attr]
                raise RuntimeError(f"[{self.node}] process died waiting for {label}: {err[:400]}")
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if stop(event):
                return event
        raise TimeoutError(f"[{self.node}] no {label} within {self.timeout}s")

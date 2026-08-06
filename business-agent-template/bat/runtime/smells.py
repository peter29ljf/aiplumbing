"""Things that are not failures but are worth seeing.

A scenario can pass and still be one nobody would want to be on the other end of: a
minute of silence, the same question twice, a tool called five times in one step. None of
these fail an assertion, and all of them are the difference between working and usable.

Nothing here decides anything. It points.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# What the customer was told is acceptable to wait. Above this they start wondering
# whether the thing is broken, and a few of them close the tab.
SLOW_TURN_SECONDS = 20.0
# The same tool inside one step. Twice is a retry; four times is a loop.
REPEATED_TOOL = 3
# How alike two of our own questions have to be before it counts as asking again.
SAME_QUESTION = 0.6


@dataclass
class Smell:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind:<16}{self.detail}"


def sniff(result: Any) -> list[Smell]:
    return [*_slow(result), *_looping(result), *_asked_again(result)]


def _slow(result: Any) -> list[Smell]:
    """Per customer turn, which is what they actually wait — not per model call."""
    found, turn, seconds = [], 0, 0.0
    seen_nodes: list[str] = []
    for step in result.steps:
        seconds += step.seconds
        seen_nodes.append(step.node)
        if step.said:
            turn += 1
            if seconds > SLOW_TURN_SECONDS:
                found.append(Smell(
                    "slow turn",
                    f"turn {turn} took {seconds:.0f}s across {len(seen_nodes)} call(s) "
                    f"in {' → '.join(dict.fromkeys(seen_nodes))}",
                ))
            seconds, seen_nodes = 0.0, []
    return found


def _looping(result: Any) -> list[Smell]:
    counts: dict[tuple[str, str], int] = {}
    for step in result.steps:
        for tool in step.tools:
            key = (step.node, tool)
            counts[key] = counts.get(key, 0) + 1
    return [
        Smell("repeated tool", f"`{tool}` called {n} times in `{node}`")
        for (node, tool), n in sorted(counts.items(), key=lambda kv: -kv[1])
        if n >= REPEATED_TOOL
    ]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 3}


def _asked_again(result: Any) -> list[Smell]:
    """Two of our messages asking much the same thing.

    Being asked the same question twice is the clearest sign nobody is listening, and it
    is the one thing a customer will not forgive — they have already answered it, so the
    only conclusion available to them is that we were not reading.
    """
    questions = [text for who, text in result.transcript
                 if who == "agent" and text and "?" in text]
    found = []
    for i, first in enumerate(questions):
        for second in questions[i + 1:]:
            a, b = _words(first), _words(second)
            if not a or not b:
                continue
            alike = len(a & b) / len(a | b)
            if alike >= SAME_QUESTION:
                found.append(Smell(
                    "asked again",
                    f"{alike:.0%} the same: {first[:70]!r} / {second[:70]!r}",
                ))
                break
    return found

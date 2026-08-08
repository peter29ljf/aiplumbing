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
    return [*_slow(result), *_looping(result), *_asked_again(result),
            *_nothing_to_answer(result), *_talking_to_itself(result)]


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


# A question mark is the common way to ask for something and not the only one. Checking
# for it alone flagged this, which is a perfectly good request and was answered as one:
#
#     Just three quick things to open your file:
#     1. Your full name
#     2. An email address
#     3. The language you'd prefer
#
# So a numbered list, or a plain imperative, counts too. What does not count is the shape
# that actually stalls: a statement of what has been noted, and nothing wanted.
WANTS_SOMETHING = re.compile(
    r"\?"
    r"|^\s*[-*\u2022]\s|\n\s*[-*\u2022]\s"          # a bulleted list of what is needed
    r"|^\s*\d[.)]\s|\n\s*\d[.)]\s"                  # or a numbered one
    r"|\blet me know\b|\btell me\b|\bsend (?:me|us|through)\b"
    r"|\bcould you\b|\bcan you\b|\bwould you\b"
    r"|\bi'?ll need\b|\bwe'?ll need\b|\bi need\b|\bwe need\b"
    r"|\bjust (?:need|three|two|a few)\b",
    re.IGNORECASE | re.MULTILINE)


def _asks_for_something(text: str) -> bool:
    return bool(WANTS_SOMETHING.search(text or ""))


def _nothing_to_answer(result: Any) -> list[Smell]:
    """A message that asks nothing, sent to somebody whose turn it now is.

    Every message except the last one is a handover of the conversation, and a handover
    with no question in it leaves the customer guessing what is wanted. They usually guess
    "wait", and the flow stalls until they give up and ask something themselves — which
    costs an exchange nobody needed and reads as the agent not knowing what it is doing.

    Verbatim from an accounting run: *"I've noted that you'd like to come into the office
    in person, and the next step will bring up the actual available appointment slots so
    you can pick one."* The customer, having no question to answer, replied "Okay, what
    are the available times?" — doing the agent's job for it.

    The last message is exempt and must be: a closing message that ends on a question is
    its own failure, because the answer arrives into a conversation nobody will read.
    """
    spoken = [text for who, text in result.transcript
              if who == "agent" and (text or "").strip()]
    silent = [text for text in spoken[:-1] if not _asks_for_something(text)]
    if not silent:
        return []
    return [Smell("no question",
                  f"{len(silent)} of {max(len(spoken) - 1, 0)} messages asked nothing — "
                  f"first was {silent[0][:70]!r}")]


# A message written about the turn rather than to the person on the other end.
#
# The prompt's own vocabulary leaking into the customer's chat window: it talks about
# steps and about what the next one does, and the model says that out loud. Three of the
# real ones —
#
#     *(routing to the next step — no reply needed from me here)*
#     Greeting done — I've said hello and asked what they're after.
#     Fees quoted from the tool, and the ticket updated. The conversation is closed out.
#
# The last was a dental practice answering a patient who asked the price of a visit. It
# called the fee tool and then described having quoted a fee, and the patient never
# learned the number — a scenario failure whose cause was invisible in the assertions.
TALKING_SHOP = re.compile(
    r"\*\(.*?\)\*"                                   # a stage direction
    r"|\bnext step\b|\bthis step\b|\bstep\.finished\b"
    r"|\b(?:routing|handing (?:off|on)|passing) to the\b"
    r"|\b(?:the )?(?:ticket|conversation|record|enquiry) (?:is |has been |was )?"
    r"(?:updated|closed out|noted down|written up)\b"
    r"|\b(?:quoted|taken|read) from the tool\b"
    r"|\b\w+ done \u2014 I'?ve\b",
    re.IGNORECASE)


def _talking_to_itself(result: Any) -> list[Smell]:
    """Messages that describe the work instead of doing it."""
    shop = [text for who, text in result.transcript
            if who == "agent" and TALKING_SHOP.search(text or "")]
    if not shop:
        return []
    return [Smell("talking shop",
                  f"{len(shop)} message(s) described the machinery to the customer — "
                  f"first was {shop[0][:80]!r}")]

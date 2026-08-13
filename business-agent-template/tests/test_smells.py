"""A message that asks nothing is a message that stalls the flow.

Every agent message except the last hands the conversation back. One with no question in
it leaves the customer guessing what is wanted, and they usually guess "wait" — so the
exchange is spent, and the next thing they say is the question the agent should have
asked. Verbatim from an accounting run:

    agent     I've noted that you'd like to come into the office in person, and the next
              step will bring up the actual available appointment slots so you can pick one.
    customer  Okay, what are the available times?

The last message is exempt, and must be: a closing message ending on a question is
answered into a conversation nobody will read again.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime.smells import _nothing_to_answer  # noqa: E402


def _sniff(*agent_lines: str):
    """One customer line between each, as a real transcript has."""
    transcript = []
    for line in agent_lines:
        transcript.append(("customer", "ok"))
        transcript.append(("agent", line))
    return [s.detail for s in _nothing_to_answer(SimpleNamespace(transcript=transcript))]


def test_a_message_that_asks_nothing_mid_flow():
    found = _sniff("I've noted that down and the next step will bring up the slots.",
                   "Here are three times — which suits? ",
                   "You're booked for Tuesday at 11:00. Nothing more to do.")

    assert found and "1 of 2" in found[0]


def test_the_closing_message_is_exempt():
    """It has the opposite rule: never finish on a question."""
    assert _sniff("Which of these times suits?",
                  "You're booked for Tuesday. No need to wait here.") == []


def test_a_conversation_where_every_message_asks_something():
    assert _sniff("What's the best number for you?",
                  "Which of these three suits?",
                  "All booked — nothing further needed.") == []


def test_several_silent_messages_are_counted_together():
    found = _sniff("Noted.", "Thanks for that.", "One moment.",
                   "You're all set, nothing more to do.")

    assert found and "3 of 3" in found[0]


def test_the_first_offender_is_quoted_so_it_can_be_found():
    found = _sniff("I have passed this along internally.",
                   "Which time works? ", "Done, nothing further.")

    assert "passed this along" in found[0]


def test_asking_without_a_question_mark_still_counts():
    """Verbatim, and it was answered correctly — the bare `?` test called it a stall."""
    assert _sniff("Just three quick things to open your file:\n\n"
                  "1. Your full name\n2. An email address\n3. Your preferred language",
                  "Which time suits?",
                  "Booked. Nothing further.") == []


def test_an_imperative_request_counts():
    assert _sniff("Let me know which of those works for you.",
                  "Booked. Nothing further.") == []


def test_noting_something_down_still_does_not():
    """The shape that actually stalls: what has been recorded, and nothing wanted."""
    found = _sniff("Thanks, Dana — your record is open.",
                   "Booked. Nothing further.")

    assert found and "1 of 1" in found[0]


def test_a_single_message_conversation_says_nothing():
    """One message is the closing message, whatever else it is."""
    assert _sniff("Thanks, we'll be in touch.") == []


def test_an_empty_reply_is_not_a_message():
    """A step that finishes silently hands straight to the next one — the customer never
    sees a turn at all, so there is nothing for them to answer or not answer."""
    transcript = [("customer", "hi"), ("agent", ""), ("customer", "hello?"),
                  ("agent", "What can we do for you?"), ("customer", "a tap"),
                  ("agent", "Booked. Nothing further.")]

    assert _nothing_to_answer(SimpleNamespace(transcript=transcript)) == []

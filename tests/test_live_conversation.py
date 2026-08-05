"""What the agent is told before the customer has said anything.

Every live channel hands us the phone number: a carrier supplies it on SMS and voice, and
the chat widget takes it on the form that opens the session. The shared rules open by
asking for it, which is right when nothing supplies it and wrong here — asking somebody who
typed their number in thirty seconds ago is the clearest possible sign nobody is listening.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.live.conversation import LiveConversation  # noqa: E402


class FakeAgent:
    def __init__(self):
        self.opening = [{"role": "system", "content": "you are intake"}]

    def initial_messages(self):
        return list(self.opening)


class FakeLLM:
    def limit(self, name, default):
        return default


class FakeWorld:
    store = None
    active_ticket_id = ""


class FakeCtx:
    world = FakeWorld()
    conversation_ended = False


def _conversation(*, channel: str, phone: str = "") -> LiveConversation:
    return LiveConversation(
        agents={"intake": FakeAgent()},
        entry_agent="intake",
        llm=FakeLLM(),
        ctx=FakeCtx(),
        channel=channel,
        phone=phone,
    )


def _opening_note(conversation: LiveConversation) -> str:
    extra = conversation.messages[1:]
    return extra[0]["content"] if extra else ""


@pytest.mark.parametrize("channel", ["chat", "sms", "voice"])
def test_a_known_number_is_handed_to_the_agent_before_the_first_message(channel: str):
    note = _opening_note(_conversation(channel=channel, phone="+16047218629"))

    assert "+16047218629" in note
    assert "Do not ask them for it" in note
    assert "crm.lookup_by_phone" in note


def test_nothing_is_asserted_when_no_number_arrived():
    """The widget can be bypassed and a blocked caller has no number. Then the rules apply
    as written and the agent asks, which is what they are for."""
    conversation = _conversation(channel="chat", phone="")

    assert conversation.messages == [{"role": "system", "content": "you are intake"}]


def test_a_carrier_number_and_a_typed_one_are_not_described_the_same_way():
    """The gate on acting without a number is code either way, so this changes nothing the
    agent does. It changes what the agent believes it can tell the customer."""
    carrier = _opening_note(_conversation(channel="sms", phone="+16047218629"))
    typed = _opening_note(_conversation(channel="chat", phone="+16047218629"))

    assert "carrier vouches for it" in carrier
    assert "claim and not proof" in typed

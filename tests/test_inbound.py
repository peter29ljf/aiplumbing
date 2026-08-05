"""The three inbound channels, and what each of them knows about who is calling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.live.server import Inbound, RateLimiter, valid_phone  # noqa: E402


class FakeConversation:
    def __init__(self):
        self.said: list[str] = []
        self.channel = ""
        self.phone = ""
        self.closed = False

    def say(self, text: str) -> str:
        self.said.append(text)
        return f"echo: {text}"


class FakeSessions:
    """Stands in for the real store — these tests are about the channel, not the agent."""

    def __init__(self):
        self.conversations: dict[str, FakeConversation] = {}
        self.calls: list[dict] = []

    def get(self, *, channel, phone="", session_id=""):
        self.calls.append({"channel": channel, "phone": phone, "session_id": session_id})
        key = phone or session_id
        return self.conversations.setdefault(key, FakeConversation())


@pytest.fixture()
def inbound() -> Inbound:
    return Inbound(FakeSessions())


# ---- chat: the anonymous one -----------------------------------------


def test_chat_will_not_spend_a_model_call_without_a_number(inbound: Inbound):
    """The gate is code, not a prompt rule the model could be talked out of."""
    code, body = inbound.chat({"session_id": "s1", "text": "my tap is dripping"})
    assert code == 400
    assert body["error"] == "phone_required"
    assert inbound.sessions.calls == []          # nothing was started, nothing was spent


def test_chat_rejects_a_number_that_is_not_a_number(inbound: Inbound):
    code, _ = inbound.chat({"session_id": "s1", "phone": "call me", "text": "hi"})
    assert code == 400


def test_chat_with_a_number_reaches_the_agent(inbound: Inbound):
    code, body = inbound.chat({"session_id": "s1", "phone": "604-721-8629", "text": "hi"})
    assert code == 200
    assert body["reply"] == "echo: hi"


def test_an_overlong_message_is_capped_rather_than_paid_for(inbound: Inbound):
    code, body = inbound.chat(
        {"session_id": "s1", "phone": "6047218629", "text": "x" * 5000}
    )
    assert code == 413
    assert inbound.sessions.calls == []


def test_a_public_endpoint_has_a_ceiling(inbound: Inbound):
    """Every message is a model turn somebody pays for."""
    payload = {"session_id": "s1", "phone": "6047218629", "text": "hi"}
    codes = [inbound.chat(payload, ip="1.2.3.4")[0] for _ in range(20)]
    assert 429 in codes


def test_one_session_being_throttled_does_not_throttle_another(inbound: Inbound):
    for _ in range(20):
        inbound.chat({"session_id": "noisy", "phone": "6047218629", "text": "hi"}, ip="1.1.1.1")
    code, _ = inbound.chat({"session_id": "quiet", "phone": "6045550000", "text": "hi"}, ip="2.2.2.2")
    assert code == 200


# ---- sms: the carrier vouches for the number -------------------------


def test_sms_answers_with_twiml(inbound: Inbound):
    code, xml = inbound.sms({"From": "+16047218629", "Body": "tap is dripping"})
    assert code == 200
    assert "<Message>echo: tap is dripping</Message>" in xml


def test_sms_uses_the_number_as_the_identity(inbound: Inbound):
    inbound.sms({"From": "+16047218629", "Body": "hello"})
    assert inbound.sessions.calls[-1] == {
        "channel": "sms", "phone": "+16047218629", "session_id": ""
    }


def test_an_empty_text_is_answered_with_silence_not_a_crash(inbound: Inbound):
    code, xml = inbound.sms({"From": "+16047218629", "Body": ""})
    assert code == 200 and "<Message>" not in xml


def test_a_reply_containing_xml_cannot_break_the_response(inbound: Inbound):
    """A customer typing a tag must not be able to reshape the TwiML we return."""
    code, xml = inbound.sms({"From": "+1604", "Body": "</Message><Hangup/>"})
    assert "<Hangup/>" not in xml
    assert "&lt;/Message&gt;" in xml


# ---- voice -----------------------------------------------------------


def test_a_new_call_is_greeted_and_listened_to(inbound: Inbound):
    code, xml = inbound.voice({"From": "+16047218629"})
    assert code == 200
    assert '<Gather input="speech"' in xml


def test_what_the_caller_said_reaches_the_agent(inbound: Inbound):
    inbound.voice({"From": "+16047218629"})
    _, xml = inbound.voice({"From": "+16047218629", "SpeechResult": "my tap is dripping"})
    assert "echo: my tap is dripping" in xml


def test_the_call_ends_when_the_agent_closes_the_process(inbound: Inbound):
    inbound.voice({"From": "+16047218629"})
    inbound.sessions.conversations["+16047218629"].closed = True
    _, xml = inbound.voice({"From": "+16047218629", "SpeechResult": "thanks, bye"})
    assert "<Hangup/>" in xml


# ---- helpers ---------------------------------------------------------


def test_a_number_is_recognised_however_it_is_typed():
    assert all(valid_phone(p) for p in
               ["6047218629", "604-721-8629", "+16047218629", "(604) 721 8629"])
    assert not any(valid_phone(p) for p in ["", "12345", "call me maybe", "604721862"])


def test_the_rate_limit_window_slides():
    limiter = RateLimiter(per_minute=2)
    assert limiter.allow("k") and limiter.allow("k")
    assert not limiter.allow("k")
    assert limiter.allow("other")

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


# ---- telegram: the technician's side ---------------------------------


class FakeSessionsWithRoster(FakeSessions):
    def __init__(self, roster=("555",), offers=None):
        super().__init__()
        self.roster = roster
        self.recorded: list[dict] = []
        self.offers = offers if offers is not None else _real_offers()

    def technician_by_chat_id(self, chat_id):
        return {"id": "t_wang", "telegram_chat_id": chat_id} if chat_id in self.roster else None

    def record_technician_message(self, *, chat_id, text):
        self.recorded.append({"chat_id": chat_id, "text": text})


def _update(chat_id="555", text="on my way", user_id="u1"):
    return {"message": {"text": text, "chat": {"id": chat_id}, "from": {"id": user_id}}}


def _real_offers():
    """A real Offers over a throwaway database — the flow is the thing under test."""
    import tempfile
    from plumbing.live.offers import Offers
    from plumbing.store import SqliteStore

    return Offers(SqliteStore(Path(tempfile.mkdtemp()) / "offers.db"))


def _inbound_with_roster(monkeypatch, roster=("555",)):
    import plumbing.integrations.telegram as tg

    monkeypatch.setattr(tg, "verify_webhook_secret", lambda provided: None)
    return Inbound(FakeSessionsWithRoster(roster))


def test_a_forged_update_cannot_drive_the_agent(monkeypatch):
    """The webhook is a public URL. Without the shared secret anyone could post to it."""
    import plumbing.integrations.telegram as tg
    from plumbing.integrations.gate import LiveToolUnavailable

    def _reject(provided):
        raise LiveToolUnavailable("secret did not match")

    monkeypatch.setattr(tg, "verify_webhook_secret", _reject)
    inbound = Inbound(FakeSessionsWithRoster())
    code, _ = inbound.telegram(_update(), secret="wrong")
    assert code == 403
    assert inbound.sessions.recorded == []


def test_a_technician_on_the_roster_is_heard(monkeypatch):
    inbound = _inbound_with_roster(monkeypatch)
    code, _ = inbound.telegram(_update(text="done, tap replaced"), secret="ok")
    assert code == 200
    assert inbound.sessions.recorded == [{"chat_id": "555", "text": "done, tap replaced"}]


def test_a_stranger_is_told_the_id_they_need_rather_than_ignored(monkeypatch):
    """Somebody who should have access needs to know what to ask for."""
    inbound = _inbound_with_roster(monkeypatch)
    replies = []
    inbound._telegram_reply = lambda chat_id, text: replies.append(text)

    code, _ = inbound.telegram(_update(chat_id="999"), secret="ok")
    assert code == 200
    assert inbound.sessions.recorded == []
    assert "999" in replies[0]


def test_a_photo_with_no_caption_does_not_crash_the_webhook(monkeypatch):
    inbound = _inbound_with_roster(monkeypatch)
    inbound._telegram_reply = lambda chat_id, text: None
    code, _ = inbound.telegram(_update(text=""), secret="ok")
    assert code == 200
    assert inbound.sessions.recorded == []

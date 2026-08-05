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


class FakeStore:
    def __init__(self):
        self.chat_sessions: dict[str, str] = {}

    def open_chat_session(self, session_id: str, phone: str) -> None:
        self.chat_sessions[session_id] = phone

    def chat_session_phone(self, session_id: str) -> str:
        return self.chat_sessions.get(session_id, "")


class FakeSessions:
    """Stands in for the real store — these tests are about the channel, not the agent."""

    def __init__(self):
        self.conversations: dict[str, FakeConversation] = {}
        self.calls: list[dict] = []
        self.store = FakeStore()

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
        self.store = self.offers.store

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


# ---- routing ----------------------------------------------------------
#
# Worth a real socket rather than calling the methods directly: the three chat endpoints
# share a prefix, and `startswith("/chat")` sends /chat/new to the handler that demands a
# phone number in the body — rejecting the one request whose whole job is to supply it.


@pytest.fixture()
def base_url(inbound: Inbound):
    import threading
    from http.server import ThreadingHTTPServer

    from plumbing.live.server import make_handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(inbound))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _post(url: str, payload: dict) -> tuple[int, dict]:
    import json as _json
    import urllib.request

    request = urllib.request.Request(
        url, data=_json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310
        return response.status, _json.loads(response.read())


def test_each_chat_endpoint_reaches_its_own_handler(base_url: str):
    code, opened = _post(f"{base_url}/chat/new", {"phone": "604-721-8629"})
    assert code == 200 and opened["session_id"]

    code, answered = _post(
        f"{base_url}/chat/message", {"session_id": opened["session_id"], "text": "hi"}
    )
    assert code == 200 and answered["reply"] == "echo: hi"


def test_a_query_string_does_not_stop_a_route_matching(base_url: str):
    code, body = _post(f"{base_url}/chat/new?utm_source=google", {"phone": "604-721-8629"})
    assert code == 200 and body["session_id"]


def test_a_path_that_merely_starts_with_chat_is_not_a_chat_endpoint(base_url: str):
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(f"{base_url}/chatterbox", {"text": "hi"})
    assert caught.value.code == 404


def test_the_widget_is_told_what_number_to_ring_when_the_chat_fails(inbound: Inbound):
    """It shows that number at the moment the chat has already broken, so it cannot be
    fetched then. A number typed into the HTML instead would be a second source of truth
    that nobody remembers to change."""
    from plumbing import config

    _, body = inbound.chat_new({"phone": "604-721-8629"})

    assert body["call_us"] == config.business_rules()["company"]["phone"]


# ---- CORS -------------------------------------------------------------
#
# The widget is served from www.smartstrategy.services and the app answers on the apex
# domain, so every call the site makes is cross-origin. Nothing said so, and the browser
# threw away answers the server had already returned 200 for. That, not the endpoint
# contract, is why the chat had never once worked.


ALLOWED = "https://www.smartstrategy.services"


@pytest.fixture()
def cors_url(inbound: Inbound):
    import threading
    from http.server import ThreadingHTTPServer

    from plumbing.live.server import make_handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(inbound, origins=[ALLOWED]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _request(url: str, method: str, origin: str | None, payload: dict | None = None):
    import json as _json
    import urllib.error
    import urllib.request

    data = _json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if origin:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers)


def test_the_preflight_is_answered(cors_url: str):
    """Posting JSON is not a simple request, so the browser preflights every message.
    BaseHTTPRequestHandler answers 501 on its own, and the real request never follows."""
    status, headers = _request(f"{cors_url}/chat/new", "OPTIONS", ALLOWED)

    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == ALLOWED
    assert "POST" in headers["Access-Control-Allow-Methods"]
    assert "Content-Type" in headers["Access-Control-Allow-Headers"]


def test_an_allowed_origin_gets_the_answer_back(cors_url: str):
    status, headers = _request(
        f"{cors_url}/chat/new", "POST", ALLOWED, {"phone": "604-721-8629"}
    )

    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == ALLOWED
    assert headers.get("Vary") == "Origin"       # or a cache serves it to somebody else


def test_a_stranger_gets_no_permission(cors_url: str):
    """The endpoint spends money on a model call and hands back a session id. `*` would
    let any page on the internet run up the bill and read the replies."""
    status, headers = _request(
        f"{cors_url}/chat/new", "POST", "https://evil.example", {"phone": "604-721-8629"}
    )

    assert "Access-Control-Allow-Origin" not in headers
    assert status == 200          # the server still answers; the browser is what refuses


def test_a_stranger_is_refused_at_the_preflight(cors_url: str):
    """Cheaper than letting them through to the request that costs something."""
    status, headers = _request(f"{cors_url}/chat/new", "OPTIONS", "https://evil.example")

    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers


def test_a_request_with_no_origin_is_unaffected(cors_url: str):
    """Twilio and Telegram post here from a server, with no Origin at all. They must not
    start failing because a browser rule was added for somebody else."""
    status, headers = _request(f"{cors_url}/chat/new", "POST", None, {"phone": "604-721-8629"})

    assert status == 200
    assert "Access-Control-Allow-Origin" not in headers

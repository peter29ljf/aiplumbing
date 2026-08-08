"""The three inbound channels, and what each of them knows about who is calling."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.live.server import Inbound, RateLimiter, valid_phone  # noqa: E402


class FakeCtx:
    """Only what the server touches. `progress` is how the widget is told what is
    happening while the agent works."""

    progress = None


class FakeConversation:
    def __init__(self):
        self.said: list[str] = []
        self.channel = ""
        self.phone = ""
        self.closed = False
        self.ctx = FakeCtx()

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


@pytest.fixture()
def sent(inbound: Inbound, monkeypatch):
    """Everything the worker texts back out, instead of reaching Twilio."""
    outbox: list[tuple[str, str]] = []
    monkeypatch.setattr(inbound, "_send_sms", lambda to, body: outbox.append((to, body)))
    return outbox


def _drain(inbound: Inbound, phone: str, timeout: float = 5.0) -> None:
    from plumbing.world import normalize_phone

    key = f"sms:{normalize_phone(phone)}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        work = inbound._pending.get(key)
        if work is not None and work.done:
            return
        time.sleep(0.01)
    raise AssertionError("the worker never finished")


def test_twilio_is_answered_at_once_and_the_reply_follows(inbound: Inbound, sent):
    """Twilio wants TwiML within fifteen seconds and the agent takes minutes. The reply
    cannot ride back on this response, so it goes out as a new message."""
    code, xml = inbound.sms({"From": "+16047218629", "Body": "tap is dripping"})

    assert code == 200
    assert "<Message>" not in xml           # nothing to say yet, and that is the point

    _drain(inbound, "+16047218629")
    assert sent == [("+16047218629", "echo: tap is dripping")]


def test_sms_uses_the_number_as_the_identity(inbound: Inbound, sent):
    inbound.sms({"From": "+16047218629", "Body": "hello"})
    _drain(inbound, "+16047218629")

    assert inbound.sessions.calls[-1] == {
        "channel": "sms", "phone": "+16047218629", "session_id": ""
    }


def test_an_empty_text_is_answered_with_silence_not_a_crash(inbound: Inbound, sent):
    code, xml = inbound.sms({"From": "+16047218629", "Body": ""})

    assert code == 200 and "<Message>" not in xml
    assert sent == []


def test_a_second_text_arriving_mid_thought_is_answered_too(inbound: Inbound, sent):
    """A phone has no manners: people send a second line while the first is unanswered.
    Dropping it loses what they said; running it concurrently interleaves two turns into
    one message list."""
    conversation = inbound.sessions.get(channel="sms", phone="+16047218629")
    started, release = threading.Event(), threading.Event()

    def slow(text: str) -> str:
        if not started.is_set():
            started.set()
            release.wait(5)
        return "echo: " + text

    conversation.say = slow

    inbound.sms({"From": "+16047218629", "Body": "first"})
    started.wait(5)
    inbound.sms({"From": "+16047218629", "Body": "and also this"})
    release.set()
    _drain(inbound, "+16047218629")

    assert [body for _, body in sent] == ["echo: first", "echo: and also this"]


def test_a_worker_that_blows_up_still_says_something(inbound: Inbound, sent):
    """Otherwise the customer's text is simply never answered and nothing anywhere says so."""
    conversation = inbound.sessions.get(channel="sms", phone="+16047218629")

    def explode(text: str) -> str:
        raise RuntimeError("the model fell over")

    conversation.say = explode
    inbound.sms({"From": "+16047218629", "Body": "hello"})
    _drain(inbound, "+16047218629")

    assert len(sent) == 1
    assert "went wrong" in sent[0][1]


def test_a_reply_containing_xml_cannot_break_the_response(inbound: Inbound):
    """A customer typing a tag must not be able to reshape the TwiML we return. The
    acknowledgement carries no text now, but _twiml is still what answers the voice
    endpoint, where the agent's words do go into the markup."""
    from plumbing.live.server import _twiml

    xml = _twiml("</Message><Hangup/>")

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


def _wait_over_http(base_url: str, session_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, polled = _post(f"{base_url}/chat/poll", {"session_id": session_id})
        if polled["status"] != "working":
            return polled
        time.sleep(0.01)
    raise AssertionError("the worker never finished")


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

    code, accepted = _post(
        f"{base_url}/chat/message", {"session_id": opened["session_id"], "text": "hi"}
    )
    assert code == 202 and accepted["status"] == "working"

    assert _wait_over_http(base_url, opened["session_id"]) == {
        "status": "ready", "reply": "echo: hi",
    }


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


# ---- opening a session ------------------------------------------------


def test_opening_a_session_costs_nothing(inbound: Inbound):
    """The number is taken on the form. No model call happens until they say something."""
    code, body = inbound.chat_new({"phone": "604-721-8629"})

    assert code == 200
    assert body["session_id"] and body["greeting"]
    assert inbound.sessions.calls == []


def test_a_session_cannot_be_opened_without_a_number(inbound: Inbound):
    code, body = inbound.chat_new({"phone": ""})

    assert code == 400
    assert body["error"] == "phone_required"
    assert inbound.sessions.store.chat_sessions == {}


def test_two_sessions_never_collide(inbound: Inbound):
    """The id is minted here. A browser choosing its own could choose somebody else's."""
    first = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]
    second = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]

    assert first != second


def test_a_session_the_server_has_never_heard_of_is_told_to_start_again(inbound: Inbound):
    """What a widget left open across a restart sees. A dead box is worse than saying so."""
    code, body = inbound.chat_message({"session_id": "made-up", "text": "hello"})

    assert code == 404
    assert body["error"] == "unknown_session"
    assert inbound.sessions.calls == []


def test_the_message_endpoint_keeps_the_ceiling(inbound: Inbound):
    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]

    code, _ = inbound.chat_message({"session_id": session_id, "text": "x" * 1001})

    assert code == 413


# ---- answering later --------------------------------------------------
#
# The first turn of a conversation was measured at 129 seconds — the agent looks up the
# customer, reads the rules, checks the diary and prices the call-out. Cloudflare cuts an
# idle connection at 100, so the reply was written, stored, and never seen: the browser's
# fetch rejected and the widget told the customer we were offline while the agent was
# still working.


def _wait(inbound: Inbound, session_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, polled = inbound.chat_poll({"session_id": session_id})
        if polled["status"] != "working":
            return polled
        time.sleep(0.01)
    raise AssertionError("the worker never finished")


def test_a_message_is_accepted_before_the_agent_has_answered(inbound: Inbound):
    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]

    code, body = inbound.chat_message({"session_id": session_id, "text": "my tap drips"})

    assert code == 202
    assert body["status"] == "working"


def test_the_reply_arrives_on_a_later_poll(inbound: Inbound):
    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]
    inbound.chat_message({"session_id": session_id, "text": "my tap drips"})

    assert _wait(inbound, session_id) == {"status": "ready", "reply": "echo: my tap drips"}
    # The number came from the session, not from the request that could have said anything.
    assert inbound.sessions.calls == [
        {"channel": "chat", "phone": "604-721-8629", "session_id": session_id}
    ]


def test_a_number_in_the_message_body_cannot_change_whose_history_is_read(inbound: Inbound):
    """Otherwise editing one field of a request reads another customer's record."""
    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]

    inbound.chat_message(
        {"session_id": session_id, "phone": "778-555-0000", "text": "hello"}
    )
    _wait(inbound, session_id)

    assert inbound.sessions.calls[0]["phone"] == "604-721-8629"


def test_a_reply_is_handed_over_once(inbound: Inbound):
    """Two polls crossing in flight must not print the answer twice."""
    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]
    inbound.chat_message({"session_id": session_id, "text": "hello"})
    _wait(inbound, session_id)

    _, again = inbound.chat_poll({"session_id": session_id})

    assert again == {"status": "idle"}


def test_a_second_message_is_refused_while_the_first_is_still_working(inbound: Inbound):
    """LiveConversation holds the message list. Two workers appending to it would
    interleave one customer's sentence into the middle of another's."""
    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]
    started = threading.Event()
    release = threading.Event()

    conversation = inbound.sessions.get(channel="chat", phone="604-721-8629",
                                        session_id=session_id)

    def slow(text: str) -> str:
        started.set()
        release.wait(5)
        return "echo: " + text

    conversation.say = slow
    inbound.sessions.calls.clear()
    inbound.chat_message({"session_id": session_id, "text": "first"})
    started.wait(5)

    code, body = inbound.chat_message({"session_id": session_id, "text": "second"})
    release.set()

    assert code == 409
    assert body["error"] == "still_working"


def test_a_worker_that_blows_up_does_not_leave_the_widget_polling_forever(inbound: Inbound):
    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]
    conversation = inbound.sessions.get(channel="chat", phone="604-721-8629",
                                        session_id=session_id)

    def explode(text: str) -> str:
        raise RuntimeError("the model fell over")

    conversation.say = explode
    inbound.chat_message({"session_id": session_id, "text": "hello"})

    answer = _wait(inbound, session_id)

    assert answer["status"] == "error"
    assert "RuntimeError" == answer["detail"]
    assert "stack" not in answer["message"].lower()      # the customer gets an apology


def test_polling_a_session_nobody_has_written_to_is_idle_not_an_error(inbound: Inbound):
    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]

    code, body = inbound.chat_poll({"session_id": session_id})

    assert code == 200
    assert body == {"status": "idle"}


def test_both_spellings_of_the_twilio_webhook_are_accepted(base_url: str):
    """The numbers were inherited pointing at /twilio/sms while the server served /sms.
    Inbound texts were a 404 and nothing on this side said so — the sender simply got no
    answer. The console and the code are edited by different hands on different days."""
    import urllib.parse
    import urllib.request

    for path in ("/sms", "/twilio/sms", "/voice", "/twilio/voice"):
        data = urllib.parse.urlencode({"From": "+16047218629", "Body": "hi"}).encode()
        request = urllib.request.Request(f"{base_url}{path}", data=data, method="POST")
        with urllib.request.urlopen(request) as response:  # noqa: S310
            assert response.status == 200, path
            assert b"<Response>" in response.read(), path


def test_the_widget_is_told_what_the_agent_is_doing(inbound: Inbound):
    """A minute of three dots looks the same as a system that has died."""
    from plumbing.live.server import DOING

    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]
    conversation = inbound.sessions.get(channel="chat", phone="604-721-8629",
                                        session_id=session_id)
    started, release = threading.Event(), threading.Event()

    def slow(text: str) -> str:
        conversation.progress("calendar.find_slots")
        started.set()
        release.wait(5)
        return "echo: " + text

    conversation.say = slow
    inbound.chat_message({"session_id": session_id, "text": "when can you come?"})
    started.wait(5)

    _, polled = inbound.chat_poll({"session_id": session_id})
    release.set()

    assert polled["status"] == "working"
    assert polled["doing"] == DOING["calendar.find_slots"]


def test_a_tool_with_no_wording_does_not_leak_its_name_to_the_customer(inbound: Inbound):
    from plumbing.live.server import DOING_FALLBACK

    session_id = inbound.chat_new({"phone": "604-721-8629"})[1]["session_id"]
    conversation = inbound.sessions.get(channel="chat", phone="604-721-8629",
                                        session_id=session_id)
    started, release = threading.Event(), threading.Event()

    def slow(text: str) -> str:
        conversation.progress("ticket.set_fields")     # deliberately not in DOING
        started.set()
        release.wait(5)
        return "echo: " + text

    conversation.say = slow
    inbound.chat_message({"session_id": session_id, "text": "hello"})
    started.wait(5)

    _, polled = inbound.chat_poll({"session_id": session_id})
    release.set()

    assert polled["doing"] == DOING_FALLBACK
    assert "ticket" not in polled["doing"]


def test_every_phrase_is_reachable_from_the_name_the_agent_reports():
    """The model is given the wire name — `crm_lookup_by_phone` — because OpenAI function
    names may not contain dots, and the wording is keyed on the dotted names people write.
    Keyed one way and read the other, every tool missed and every customer saw the
    fallback: the feature looked like it was working and was doing nothing."""
    from flow.sim import tools
    from plumbing.live.server import DOING, _doing

    known = tools.names()
    for tool in DOING:
        assert tool in known, f"{tool} is not one of the flow's tools"
        wire = tool.replace(".", "_", 1)
        assert _doing(wire) == DOING[tool], f"{tool} unreachable as {wire}"
        assert _doing(tool) == DOING[tool]

    assert _doing("ticket_set_fields") is None       # no line of its own
    assert _doing("step_finished") is None


def test_every_tool_the_customer_waits_on_has_something_to_say():
    """A tool with no wording shows the generic line, which is a wait nobody explained.

    Bookkeeping is exempt: nothing is happening on the customer's behalf while a field is
    written down, and giving those a line means the last thing on screen is "Making a
    note" — every batch ends on one.
    """
    from flow.sim import tools
    from plumbing.live.server import DOING

    silent = {"ticket.set_fields", "step.finished"}
    assert sorted(tools.names() - silent - set(DOING)) == []


def test_a_bookkeeping_call_does_not_wipe_out_the_line_before_it():
    """The agent asks for several tools at once and this is called for each. The real
    first batch is [crm_lookup_by_phone, ticket_create]; when the unnamed one overwrote
    the named one, the fallback was all any customer ever saw for a whole turn — and it
    looked exactly like the feature working."""
    from plumbing.live.server import DOING, _Pending

    work = _Pending()
    work.note_tool("crm_lookup_by_phone")
    work.note_tool("ticket_create")

    assert work.doing == DOING["crm.lookup_by_phone"]

    work.note_tool("ticket_update_status")
    assert work.doing == DOING["crm.lookup_by_phone"]      # still the last one that meant something

    work.note_tool("calendar_find_slots")
    work.note_tool("ticket_set_fields")
    assert work.doing == DOING["calendar.find_slots"]      # moves on when there is news


def test_nothing_worth_saying_yet_still_says_something(inbound: Inbound):
    """Before any tool with a line of its own has run, the poll must still answer."""
    from plumbing.live.server import DOING_FALLBACK, _Pending

    work = _Pending()
    work.note_tool("ticket_create")

    assert work.doing == ""
    inbound._pending["s"] = work
    _, polled = inbound.chat_poll({"session_id": "s"})
    assert polled["doing"] == DOING_FALLBACK


def test_every_phrase_reads_like_something_a_person_would_say():
    """These go on a customer's screen. A tool name showing through is the failure."""
    from plumbing.live.server import DOING, DOING_FALLBACK

    for tool, phrase in DOING.items():
        assert phrase[0].isupper(), tool
        assert "." not in phrase and "_" not in phrase, tool
    assert DOING_FALLBACK


def test_the_widget_is_served_by_the_app(base_url: str):
    """It was being edited in place on the server with scp, so the version customers ran
    existed nowhere else and no change to it was reviewable."""
    import urllib.request

    with urllib.request.urlopen(f"{base_url}/chat/widget.js") as response:  # noqa: S310
        body = response.read().decode()
        assert response.status == 200
        assert "javascript" in response.headers["Content-Type"]
        assert "max-age" in response.headers.get("Cache-Control", "")

    assert "/chat/new" in body and "/chat/poll" in body

"""The three ways a customer reaches us: web chat, SMS and a phone call.

One agent behind all of them. What differs is what arrives with the message.

**SMS and voice** come through a carrier, which vouches for the number. The conversation
is that number's, and it is the same conversation whether they text today and ring
tomorrow.

**Web chat** is open to the anonymous internet and brings nothing with it. Two problems
follow, and both are handled here in code rather than in a prompt:

- *Cost.* Every message is a model turn somebody pays for. A public endpoint with no
  ceiling is a bill waiting to happen, so there is a length cap and a per-session and
  per-address rate limit.
- *Identity.* A number typed into a form is a **claim**, not proof — nothing vouches for
  it the way a carrier does. Chat is therefore keyed by session, with the asserted number
  carried alongside. The gate that requires one before any message is accepted is code on
  purpose: history, warranty and booking all hang off that number, and a rule the model
  can be talked out of is not a gate.

Written on `http.server` like the console, so running this adds no dependency. It is
enough for a single business behind nginx; it is not a general web framework and does not
try to be.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
import urllib.parse
from collections import defaultdict, deque
from pathlib import Path
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from xml.sax.saxutils import escape

from plumbing.live.sessions import SessionStore
from plumbing.world import normalize_phone

MAX_MESSAGE_CHARS = 1000
CHAT_MESSAGES_PER_MINUTE = 12
CHAT_MESSAGES_PER_MINUTE_PER_IP = 30

# North American numbers; the business serves British Columbia. Anything that reduces to
# ten digits is accepted and normalised further down.
_TEN_DIGITS = re.compile(r"^\+?1?\d{10}$")

# What the widget shows before the first model call, so opening a chat costs nothing.
CHAT_GREETING = (
    "Thanks — you're through to Fangxin Plumbing. What can we help you with?"
)

# The whole JSON surface. `/chat` is the original single-shot form: it blocks until the
# agent answers, which is fine for a script on the same machine and wrong through a proxy.
_CHAT_ROUTES = {
    "/chat": "chat",
    "/chat/new": "chat_new",
    "/chat/message": "chat_message",
    "/chat/poll": "chat_poll",
}

# Twilio's webhooks are configured in Twilio's console, not here, and the numbers were
# inherited already pointing at /twilio/sms and /twilio/voice. Inbound texts were a 404
# and nothing on this side said so — the sender simply got no answer. Both spellings are
# accepted, because the console and the code are edited by different hands on different
# days and the one that gets forgotten should not be the one that takes texts.
_SMS_ROUTES = {"/sms", "/twilio/sms"}
_VOICE_ROUTES = {"/voice", "/twilio/voice"}


# What to show a customer while the agent is working. A minute of three dots looks the
# same as a system that has died; this is the difference between waiting and wondering.
# Anything not named here falls back to the generic line — a missing entry should never
# leak a tool name onto a customer's screen.
DOING = {
    "crm.lookup_by_phone": "Looking up your details",
    "crm.create_customer": "Setting up your record",
    "crm.update_customer": "Saving your details",
    "crm.get_warranty_candidates": "Checking what we've done for you before",
    "calendar.find_slots": "Checking the calendar",
    "calendar.create_appointment": "Booking that in",
    "calendar.reschedule": "Moving your appointment",
    "calendar.cancel": "Cancelling that appointment",
    "rules.get_standard_service_fee": "Checking what the visit costs",
    "rules.get_emergency_fee": "Checking the emergency rate",
    "rules.get_job_sizing": "Working out what's involved",
    "rules.get_safety_advisory": "Checking the safety guidance",
    "rules.check_service_eligibility": "Checking we can take this on",
    "rules.get_schedule_policy": "Checking our working hours",
    "sms.send": "Sending your confirmation",
    "email.send": "Sending that email",
    "email.request_materials": "Sending you a note about the photos",
    "escalate.raise": "Passing this to the technician",
    "handoff.transfer": "Bringing in a colleague",
}
DOING_FALLBACK = "Just a moment"


@dataclass
class _Pending:
    """One turn being worked on. Read from the HTTP thread, written by the worker."""

    done: bool = False
    doing: str = ""
    reply: str | None = None
    error: str | None = None
    # Texts that arrived while this turn was running. Chat cannot produce these (the
    # widget refuses to send while it is waiting); a phone has no such manners.
    queued: list[str] = field(default_factory=list)


class RateLimiter:
    """A sliding window per key. Small, in-process, and enough for one business."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return False
        window.append(now)
        return True


def _company_phone() -> str:
    """The number a customer should ring when the chat cannot help them."""
    from plumbing import config  # noqa: PLC0415

    return str(config.business_rules()["company"].get("phone", ""))


def valid_phone(raw: str) -> bool:
    return bool(_TEN_DIGITS.match(re.sub(r"[\s()\-.]", "", raw or "")))


class Inbound:
    """Channel handling, with no HTTP in it — so it can be tested without a socket."""

    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions
        self.per_session = RateLimiter(CHAT_MESSAGES_PER_MINUTE)
        self.per_ip = RateLimiter(CHAT_MESSAGES_PER_MINUTE_PER_IP)
        # Turns in flight, keyed by session. In memory on purpose: a reply nobody collected
        # before a restart is gone, and the customer sends their message again — which is
        # the same thing that already happens to the conversation itself.
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    # ---- web chat ----------------------------------------------------
    def chat_new(self, payload: dict[str, Any], *, ip: str = "") -> tuple[int, dict[str, Any]]:
        """Open a session. The one place the widget asks for a phone number.

        Taking it here rather than mid-conversation is the difference between a field on
        the form somebody is already filling in and a demand that interrupts them once they
        have started describing a leak. It also costs nothing: no model call happens until
        the first message, so opening a session somebody abandons is free.

        The session id is minted here rather than accepted from the caller. A browser that
        picks its own could pick one already in use and walk into somebody else's
        conversation.
        """
        phone = str(payload.get("phone") or "").strip()
        if not valid_phone(phone):
            return 400, {
                "error": "phone_required",
                "message": "Please enter a phone number we can reach you on to start the chat.",
            }
        if not self.per_ip.allow(ip or "anon"):
            return 429, {"error": "rate_limited", "message": "Too many requests. One moment."}

        session_id = uuid.uuid4().hex
        self.sessions.store.open_chat_session(session_id, phone)
        return 200, {
            "session_id": session_id,
            "greeting": CHAT_GREETING,
            # For the widget's "we are offline, call us instead" line. It comes from here
            # rather than being typed into the page, because a number hardcoded in HTML is
            # a second source of truth that nobody remembers to change — and the one place
            # it appears is the moment the chat has already failed.
            "call_us": _company_phone(),
        }

    def chat_message(self, payload: dict[str, Any], *, ip: str = "") -> tuple[int, dict[str, Any]]:
        """Take the message and answer later. Carries no number of its own.

        The number comes from the session, so a caller cannot change whose history they
        are reading by editing one field of the next request.

        **The reply does not come back on this request.** The first turn of a conversation
        is the agent looking up the customer, reading the rules, checking the diary and
        pricing the call-out — measured at 129 seconds. Cloudflare cuts an idle connection
        at 100, so the answer was written, stored, and never seen: the browser's fetch
        rejected and the widget told the customer we were offline while the agent was
        still working. Holding an HTTP request open for two minutes is a bet on every
        proxy between here and the customer, and it is a bet that loses.

        So this returns at once and the front end polls `/chat/poll`. Nothing in the chain
        has to wait, and how long the agent takes stops being a networking question.
        """
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return 400, {"error": "session_id is required"}

        phone = self.sessions.store.chat_session_phone(session_id)
        if not phone:
            # Also what a widget left open across a restart sees. Saying so plainly lets
            # the front end open a fresh session instead of showing a dead box.
            return 404, {
                "error": "unknown_session",
                "message": "This chat has expired. Start a new one.",
            }

        text = str(payload.get("text") or "").strip()
        if not text:
            return 400, {"error": "text is required"}
        if len(text) > MAX_MESSAGE_CHARS:
            return 413, {"error": "too_long", "limit": MAX_MESSAGE_CHARS}
        if not self.per_session.allow(session_id) or not self.per_ip.allow(ip or session_id):
            return 429, {"error": "rate_limited", "message": "Too many messages. One moment."}

        with self._lock:
            running = self._pending.get(session_id)
            if running is not None and not running.done:
                # One turn at a time per session. LiveConversation holds the message list,
                # and two workers appending to it would interleave one customer's sentence
                # into the middle of another's.
                return 409, {
                    "error": "still_working",
                    "message": "Still working on your last message — one moment.",
                }
            work = _Pending()
            self._pending[session_id] = work

        thread = threading.Thread(
            target=self._run_turn, args=(session_id, phone, text, work), daemon=True
        )
        thread.start()
        return 202, {"status": "working", "session_id": session_id}

    def _run_turn(self, session_id: str, phone: str, text: str, work: _Pending) -> None:
        try:
            conversation = self.sessions.get(channel="chat", phone=phone, session_id=session_id)
            conversation.ctx.progress = lambda tool: setattr(
                work, "doing", DOING.get(tool, DOING_FALLBACK)
            )
            work.reply = conversation.say(text)
        except Exception as exc:  # noqa: BLE001
            # The customer gets an apology, not a stack trace, and the type is kept so the
            # log has something to go on. A worker that dies silently leaves the front end
            # polling forever.
            work.error = type(exc).__name__
        finally:
            work.done = True

    def chat_poll(self, payload: dict[str, Any], *, ip: str = "") -> tuple[int, dict[str, Any]]:
        """Is the reply ready? Cheap, and deliberately not rate-limited with the messages.

        Polling is not the customer talking; throttling it would only make the widget look
        broken while the agent was working perfectly well.
        """
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return 400, {"error": "session_id is required"}

        with self._lock:
            work = self._pending.get(session_id)
            if work is None:
                return 200, {"status": "idle"}
            if not work.done:
                return 200, {"status": "working", "doing": work.doing or DOING_FALLBACK}
            # Handed over once, then forgotten — otherwise a reply is delivered twice when
            # two polls cross in flight.
            del self._pending[session_id]

        if work.error:
            return 200, {
                "status": "error",
                "message": "Sorry — something went wrong at our end. Please try again.",
                "detail": work.error,
            }
        return 200, {"status": "ready", "reply": work.reply or ""}

    def chat(self, payload: dict[str, Any], *, ip: str = "") -> tuple[int, dict[str, Any]]:
        session_id = str(payload.get("session_id") or "").strip()
        phone = str(payload.get("phone") or "").strip()
        text = str(payload.get("text") or "").strip()

        if not session_id:
            return 400, {"error": "session_id is required"}
        if not valid_phone(phone):
            # Deliberately before anything is spent on a model call.
            return 400, {
                "error": "phone_required",
                "message": "Please enter a phone number we can reach you on to start the chat.",
            }
        if not text:
            return 400, {"error": "text is required"}
        if len(text) > MAX_MESSAGE_CHARS:
            return 413, {"error": "too_long", "limit": MAX_MESSAGE_CHARS}
        if not self.per_session.allow(session_id) or not self.per_ip.allow(ip or session_id):
            return 429, {"error": "rate_limited", "message": "Too many messages. One moment."}

        conversation = self.sessions.get(channel="chat", phone=phone, session_id=session_id)
        return 200, {"reply": conversation.say(text), "session_id": session_id}

    # ---- SMS ---------------------------------------------------------
    def sms(self, form: dict[str, str]) -> tuple[int, str]:
        """Twilio posts a form and wants TwiML back **within fifteen seconds**.

        The agent takes considerably longer than that — measured between two and four
        minutes on a first turn — so the reply cannot ride back on this response. Twilio
        would have given up long before, and the customer would be left with a text that
        was never answered.

        So this acknowledges with empty TwiML and the answer goes out as a new outbound
        message when it is ready. To the customer that is indistinguishable from a person
        who took a minute to reply, which is what they expect from a text.
        """
        phone = (form.get("From") or "").strip()
        text = (form.get("Body") or "").strip()
        if not phone or not text:
            return 200, _twiml("")

        key = f"sms:{normalize_phone(phone)}"
        with self._lock:
            running = self._pending.get(key)
            if running is not None and not running.done:
                # They sent a second text while we were working on the first. Both are
                # theirs and both matter, so it is queued rather than dropped — but not
                # run concurrently, because one conversation has one message list.
                running.queued.append(text[:MAX_MESSAGE_CHARS])
                return 200, _twiml("")
            work = _Pending()
            self._pending[key] = work

        threading.Thread(
            target=self._run_sms, args=(phone, text[:MAX_MESSAGE_CHARS], work), daemon=True
        ).start()
        return 200, _twiml("")

    def _run_sms(self, phone: str, text: str, work: _Pending) -> None:
        """Answer a text, then send the answer as a text. Drains anything that arrived
        while we were thinking, so a customer who sends two lines gets both considered."""
        try:
            conversation = self.sessions.get(channel="sms", phone=phone)
            pending: str | None = text
            while pending is not None:
                reply = conversation.say(pending)
                if reply:
                    self._send_sms(phone, reply)
                with self._lock:
                    pending = work.queued.pop(0) if work.queued else None
                    if pending is None:
                        work.done = True
        except Exception as exc:  # noqa: BLE001
            work.error = type(exc).__name__
            self._send_sms(
                phone,
                "Sorry — something went wrong at our end. Please try again, or call us.",
            )
        finally:
            work.done = True

    def _send_sms(self, phone: str, body: str) -> None:
        """Never raises. A notification that fails must not take the conversation with it."""
        from plumbing.integrations import is_live  # noqa: PLC0415

        if not is_live("sms.send"):
            return
        try:
            from plumbing.integrations import twilio_sms  # noqa: PLC0415

            twilio_sms.send_sms(phone, body)
        except Exception:  # noqa: BLE001, S110
            pass

    # ---- voice -------------------------------------------------------
    def voice(self, form: dict[str, str]) -> tuple[int, str]:
        """A call in progress. Twilio transcribes; we answer and ask for the next thing.

        `SpeechResult` is absent on the first request of a call — that is the greeting.
        """
        phone = (form.get("From") or "").strip()
        said = (form.get("SpeechResult") or "").strip()

        if not said:
            conversation = self.sessions.get(channel="voice", phone=phone)
            spoken = conversation.say("[caller has just connected and is waiting for you to speak first]")
            return 200, _gather(spoken or "Fangxin Plumbing, how can I help?")

        conversation = self.sessions.get(channel="voice", phone=phone)
        reply = conversation.say(said[:MAX_MESSAGE_CHARS])
        if conversation.closed:
            return 200, _say_and_hang_up(reply)
        return 200, _gather(reply)


    # ---- telegram: the technician's side -----------------------------
    def telegram(self, update: dict[str, Any], *, secret: str) -> tuple[int, dict[str, Any]]:
        """A technician replying. Verified, allowlisted, and answered immediately.

        Telegram retries a webhook that is slow to answer, which would send the reply
        twice, so this returns as soon as the message is recorded rather than waiting on
        anything downstream.
        """
        from plumbing.integrations import telegram as tg  # noqa: PLC0415
        from plumbing.integrations.gate import LiveToolUnavailable  # noqa: PLC0415

        try:
            tg.verify_webhook_secret(secret)
        except LiveToolUnavailable:
            # Anyone can post here; a forged update must not be able to drive the agent.
            return 403, {"error": "forbidden"}

        if update.get("callback_query"):
            data = str(update["callback_query"].get("data") or "")
            if data.startswith("f:"):
                return self._followup_press(update["callback_query"])
            return self._button_press(update["callback_query"])

        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        chat_id = str((message.get("chat") or {}).get("id") or "")
        user_id = str((message.get("from") or {}).get("id") or "")
        if not chat_id or not user_id:
            return 200, {"ok": True}

        known = self.sessions.technician_by_chat_id(chat_id)
        if known is None:
            # Told plainly rather than ignored: somebody who should have access needs to
            # know what to ask for, and the id they need is right there.
            self._telegram_reply(chat_id, f"This bot is for our technicians. Your id is {chat_id}.")
            return 200, {"ok": True}
        if not text:
            self._telegram_reply(chat_id, "Text only for now, please.")
            return 200, {"ok": True}

        # A follow-up they were asked and have typed an answer to rather than tapping.
        outstanding = self.sessions.store.open_followup(chat_id)
        if outstanding is not None:
            self.sessions.store.update_followup(
                outstanding["followup_id"], status="answered", answer=text
            )
            self.sessions.store.add_event(
                "job_outcome_reported", ticket_id=outstanding["ticket_id"], detail=text[:200],
                followup_id=outstanding["followup_id"],
            )
            self._telegram_reply(chat_id, "Thanks — noted.")
            self.sessions.record_technician_message(chat_id=chat_id, text=text)
            return 200, {"ok": True}

        offers = self.sessions.offers
        pending = offers.awaiting_reason(chat_id)
        if pending is not None:
            # They tapped Decline and were asked why. This is the answer.
            offers.decline(pending.offer_id, text)
            self._settle(offers.get(pending.offer_id))
            self._telegram_reply(chat_id, "Noted — the office will sort it out. Thanks.")
        else:
            # An offer is on screen and they are typing. Held as a possible reason so that
            # "type why, then tap Decline" works; harmless if they tap Accept instead.
            offers.note_typed(chat_id, text)

        self.sessions.record_technician_message(chat_id=chat_id, text=text)
        return 200, {"ok": True}

    def _button_press(self, callback: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Accept is one tap. Decline needs a reason, given either before or after."""
        from plumbing.live import offers as offers_mod  # noqa: PLC0415

        callback_id = str(callback.get("id") or "")
        chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id") or "")
        parsed = offers_mod.parse_callback(str(callback.get("data") or ""))
        if parsed is None or self.sessions.technician_by_chat_id(chat_id) is None:
            self._answer(callback_id, "")
            return 200, {"ok": True}

        offer_id, decision = parsed
        offers = self.sessions.offers
        offer = offers.get(offer_id)
        if offer is None or offer.state in ("accepted", "declined"):
            # Someone scrolled back to an old offer. Answering the callback still matters
            # or their client spins.
            self._answer(callback_id, "That one is already settled.")
            return 200, {"ok": True}

        if decision == offers_mod.ACCEPT:
            offers.accept(offer_id)
            self._answer(callback_id, "Accepted — thanks.")
            self._settle(offers.get(offer_id))
            return 200, {"ok": True}

        if offer.reason:
            # They typed why before tapping Decline, which is the flow the message asks
            # for. Nothing more to ask.
            offers.decline(offer_id, offer.reason)
            self._answer(callback_id, "Declined — thanks for the reason.")
            self._settle(offers.get(offer_id))
            return 200, {"ok": True}

        # Tapped Decline with nothing typed. Asking is better than refusing the tap: the
        # person who presses the obvious button before reading is not the one at fault.
        offers.ask_for_reason(offer_id)
        self._answer(callback_id, "")
        self._telegram_reply(chat_id, "No problem — what should I tell the customer?")
        return 200, {"ok": True}

    def _followup_press(self, callback: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """The day-after answer: the work happened, or the customer decided against it.

        Those are the only two outcomes that change what the office does. Anything more
        detailed is a conversation, and the technician can type it.
        """
        from plumbing.live.reminders import OUTCOME_DECLINED, OUTCOME_DONE  # noqa: PLC0415

        callback_id = str(callback.get("id") or "")
        chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id") or "")
        parts = str(callback.get("data") or "").split(":")
        if len(parts) != 3 or self.sessions.technician_by_chat_id(chat_id) is None:
            self._answer(callback_id, "")
            return 200, {"ok": True}

        _, followup_id, outcome = parts
        answer = {OUTCOME_DONE: "done", OUTCOME_DECLINED: "customer_declined"}.get(outcome)
        if answer is None:
            self._answer(callback_id, "")
            return 200, {"ok": True}

        store = self.sessions.store
        store.update_followup(followup_id, status="answered", answer=answer)
        with store.connect() as conn:
            row = conn.execute(
                "SELECT ticket_id FROM followups WHERE followup_id = ?", (followup_id,)
            ).fetchone()
        store.add_event("job_outcome_reported", ticket_id=row["ticket_id"] if row else "",
                        detail=answer, followup_id=followup_id)
        self._answer(callback_id, "Thanks — noted.")
        return 200, {"ok": True}

    def _settle(self, offer: Any) -> None:
        """Rewrite the offer message so its buttons are gone and the outcome is visible."""
        from plumbing.integrations import is_live  # noqa: PLC0415

        if offer is None or not offer.message_id or not is_live("telegram.send"):
            return
        from plumbing.integrations import telegram as tg  # noqa: PLC0415
        from plumbing.integrations.gate import LiveToolUnavailable  # noqa: PLC0415
        from plumbing.live import offers as offers_mod  # noqa: PLC0415

        try:
            tg.edit_message(offer.chat_id, offer.message_id, offers_mod.settled_text(offer))
        except LiveToolUnavailable:
            return

    def _answer(self, callback_id: str, text: str) -> None:
        from plumbing.integrations import is_live  # noqa: PLC0415

        if not callback_id or not is_live("telegram.send"):
            return
        from plumbing.integrations import telegram as tg  # noqa: PLC0415
        from plumbing.integrations.gate import LiveToolUnavailable  # noqa: PLC0415

        try:
            tg.answer_callback(callback_id, text)
        except LiveToolUnavailable:
            return

    def _telegram_reply(self, chat_id: str, text: str) -> None:
        from plumbing.integrations import is_live  # noqa: PLC0415

        if not is_live("telegram.send"):
            return
        from plumbing.integrations import telegram as tg  # noqa: PLC0415
        from plumbing.integrations.gate import LiveToolUnavailable  # noqa: PLC0415

        try:
            tg.send_message(chat_id, text)
        except LiveToolUnavailable:
            return


# ---- TwiML ------------------------------------------------------------


def _twiml(body: str) -> str:
    inner = f"<Message>{escape(body)}</Message>" if body else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>'


def _gather(text: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Gather input="speech" speechTimeout="auto" action="/voice" method="POST">'
        f"<Say>{escape(text)}</Say>"
        "</Gather>"
        "<Say>Sorry, I did not catch that. Please call again or send us a text.</Say>"
        "</Response>"
    )


def _say_and_hang_up(text: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"<Say>{escape(text)}</Say><Hangup/></Response>"
    )


# ---- HTTP -------------------------------------------------------------


def make_handler(
    inbound: Inbound, origins: list[str] | None = None
) -> type[BaseHTTPRequestHandler]:
    """`origins` may call the chat endpoints from a browser; None reads them from config.

    Resolved once here rather than per request: an allow-list that can change under a
    running server is one more thing that behaves differently at 3am than it did in a test.
    """
    from plumbing import config  # noqa: PLC0415

    allowed_origins = frozenset(config.web_chat_origins() if origins is None else origins)

    class Handler(BaseHTTPRequestHandler):
        server_version = "FangxinInbound/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            return

        def _client_ip(self) -> str:
            # Behind nginx the socket address is always the proxy, so prefer the header it
            # sets. Only trust it because nothing else can reach this port.
            return (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() \
                or self.client_address[0]

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _allow_origin(self) -> str:
            """The Origin header echoed back, if it is one we allow. Otherwise nothing.

            Echoed rather than answered with `*`: these endpoints spend money on a model
            call and carry a session id, so any page on the internet being able to run up
            the bill and read the replies is not a trade worth making. Echoing also keeps
            the door open for credentialed requests later, which `*` forbids outright.
            """
            origin = self.headers.get("Origin", "")
            return origin if origin and origin in allowed_origins else ""

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            allowed = self._allow_origin()
            if allowed:
                self.send_header("Access-Control-Allow-Origin", allowed)
                # Caches and proxies must not serve one origin's answer to another.
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            """The preflight. Without it BaseHTTPRequestHandler answers 501 and the
            browser never sends the real request — which is what was happening: posting
            JSON is not a simple request, so every chat message was preflighted first."""
            allowed = self._allow_origin()
            if not allowed:
                self._send(403, b"", "text/plain")
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            route = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
            if route == "/health":
                self._send(200, b'{"ok":true}', "application/json")
            elif route == "/chat/widget.js":
                # Served from here rather than copied into the marketing page, so the chat
                # can be fixed without touching the site and the version customers are
                # running is the one in git. Cached briefly for the same reason.
                widget = Path(__file__).parent / "static" / "widget.js"
                try:
                    body = widget.read_bytes()
                except OSError:
                    self._send(404, b"not found", "text/plain")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=300")
                allowed = self._allow_origin()
                if allowed:
                    self.send_header("Access-Control-Allow-Origin", allowed)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            # Matched exactly, on the path with any query string removed. Prefix matching
            # would send /chat/new to the handler for /chat, which wants a phone number in
            # the body and would reject the request that exists to supply one.
            route = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
            try:
                if route in _CHAT_ROUTES:
                    payload = json.loads(self._body() or b"{}")
                    handler = getattr(inbound, _CHAT_ROUTES[route])
                    code, data = handler(payload, ip=self._client_ip())
                    self._send(code, json.dumps(data).encode(), "application/json")
                elif route in _SMS_ROUTES:
                    form = _parse_form(self._body())
                    code, xml = inbound.sms(form)
                    self._send(code, xml.encode(), "application/xml")
                elif self.path.startswith("/telegram"):
                    update = json.loads(self._body() or b"{}")
                    code, data = inbound.telegram(
                        update,
                        secret=self.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
                    )
                    self._send(code, json.dumps(data).encode(), "application/json")
                elif route in _VOICE_ROUTES:
                    form = _parse_form(self._body())
                    code, xml = inbound.voice(form)
                    self._send(code, xml.encode(), "application/xml")
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as exc:  # noqa: BLE001
                # A stack trace must never reach a customer, and a dropped connection is
                # worse than a plain apology — Twilio retries on a 500.
                self._send(
                    200,
                    json.dumps({"error": "internal", "detail": type(exc).__name__}).encode()
                    if self.path.startswith("/chat")
                    else _twiml("Sorry — something went wrong here. Please try again shortly.").encode(),
                    "application/json" if self.path.startswith("/chat") else "application/xml",
                )

    return Handler


def _parse_form(body: bytes) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(body.decode(), keep_blank_values=True).items()}


def serve(database_path: str, port: int = 8770) -> None:
    from plumbing import config  # noqa: PLC0415
    from plumbing.live.reminders import ReminderLoop  # noqa: PLC0415

    sessions = SessionStore(database_path)
    inbound = Inbound(sessions)

    # Nudges an offer nobody has answered. Its own thread, and a daemon one: it must
    # never be the reason the server will not shut down, and never the reason it falls
    # over — whether a reminder went out has nothing to do with answering a customer.
    ReminderLoop(sessions.offers, config.business_rules()).start()

    ThreadingHTTPServer(("127.0.0.1", port), make_handler(inbound)).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/plumbing.db")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    serve(args.db, args.port)

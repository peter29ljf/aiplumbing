"""The web chat a customer actually types into.

Ported from the first generation's `src/plumbing/live/server.py`, which served real
customers. Almost everything here is a scar, and the shape is the important part:

**Three endpoints, not one.** The first turn of a conversation is the agent looking up the
customer, reading the rules, checking the diary and pricing the call-out — measured at 129
seconds. Cloudflare cuts an idle connection at 100. So the reply was written, stored, and
never seen: the browser's fetch rejected and the widget told the customer we were offline
while the agent was still working. Holding an HTTP request open for two minutes is a bet on
every proxy between here and the customer, and it is a bet that loses.

    POST /chat/new      { phone }            -> { session_id, greeting, call_us }
    POST /chat/message  { session_id, text } -> 202 { status: "working" }
    POST /chat/poll     { session_id }       -> { status: working | ready | error | idle }

**A number typed into a form is a claim, not proof.** SMS and voice come through a carrier
which vouches for the number; the open internet vouches for nothing. So chat is keyed by
session with the asserted number carried alongside, and the gate that requires one before
any message is accepted is code rather than a rule in a prompt — history, warranty and
booking all hang off that number, and a rule the model can be talked out of is not a gate.

**Every message is a model turn somebody pays for.** Hence the length cap and the two rate
limiters, and hence CORS that echoes a named origin rather than answering `*`.

Written on `http.server`, so running this adds no dependency. Enough for one business
behind nginx; not a web framework, and not trying to be.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bat.live.sessions import Sessions

MAX_MESSAGE_CHARS = 1000
CHAT_MESSAGES_PER_MINUTE = 12
CHAT_MESSAGES_PER_MINUTE_PER_IP = 30

# North American numbers; the business serves British Columbia.
_TEN_DIGITS = re.compile(r"^\+?1?\d{10}$")

_ROUTES = {"/chat/new": "chat_new", "/chat/message": "chat_message",
           "/chat/poll": "chat_poll"}

# What to show while the agent is working. A minute of three dots looks the same as a
# system that has died; this is the difference between waiting and wondering.
#
# Keyed on the dotted names and looked up on both spellings, because the engine reports
# whichever the model was given. The first version keyed one way and read the other, so
# every tool missed and every customer saw the fallback.
DOING = {
    "crm.lookup_by_phone": "Looking up your details",
    "crm.create_customer": "Setting up your record",
    "crm.get_warranty_candidates": "Checking what we've done for you before",
    "calendar.find_slots": "Checking the calendar",
    "calendar.create_appointment": "Booking that in",
    "calendar.find_booking": "Finding your appointment",
    "clock.now": "Checking the time",
    "rules.get_service_options": "Checking what the visit costs",
    "rules.get_job_sizing": "Working out what's involved",
    "rules.get_safety_advisory": "Checking the safety guidance",
    "sms.send": "Sending your confirmation",
    "technician.notify": "Passing this to the technician",
    "escalate.raise": "Passing this to the technician",
    "schedule.create_followup": "Arranging the follow-up",
}
DOING_FALLBACK = "Just a moment"


def _doing(tool: str) -> str:
    """The line for a tool, or empty. **Empty rather than the fallback**, deliberately.

    A step asks for several tools and this is called for each. Returning the fallback for
    the unnamed ones meant a batch ending on `ticket.set_fields` wiped out the
    `crm.lookup_by_phone` before it — and almost every batch ends on a bookkeeping call, so
    the fallback was all anyone ever saw. The caller keeps the last line that meant
    something.
    """
    return DOING.get(tool) or DOING.get(tool.replace("_", ".", 1)) or ""


@dataclass
class _Pending:
    """One turn in flight. Read from the HTTP thread, written by the worker."""

    done: bool = False
    doing: str = ""
    reply: str | None = None
    error: str | None = None
    closed: bool = False

    def note_tool(self, tool: str) -> None:
        if (phrase := _doing(tool)):
            self.doing = phrase


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


def valid_phone(raw: str) -> bool:
    return bool(_TEN_DIGITS.match(re.sub(r"[\s()\-.]", "", raw or "")))


class Inbound:
    """Channel handling with no HTTP in it, so it can be tested without a socket."""

    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions
        self.per_session = RateLimiter(CHAT_MESSAGES_PER_MINUTE)
        self.per_ip = RateLimiter(CHAT_MESSAGES_PER_MINUTE_PER_IP)
        # Turns in flight. In memory on purpose: a reply nobody collected before a restart
        # is gone and the customer sends their message again, which is a far smaller loss
        # than the conversation itself — and that is in the database.
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def chat_new(self, payload: dict[str, Any], *,
                 ip: str = "") -> tuple[int, dict[str, Any]]:
        """Open a session. The one place the widget asks for a phone number.

        Asking here rather than mid-conversation is the difference between a field on a
        form somebody is already filling in and a demand that interrupts them once they
        have started describing a leak. It costs nothing either: no model call happens
        until the first message, so a session somebody abandons is free.

        The id is minted here rather than accepted from the caller — a browser that picked
        its own could pick one in use and walk into somebody else's conversation.
        """
        phone = str(payload.get("phone") or "").strip()
        if not valid_phone(phone):
            return 400, {"error": "phone_required",
                         "message": "Please enter a phone number we can reach you on to "
                                    "start the chat."}
        if not self.per_ip.allow(ip or "anon"):
            return 429, {"error": "rate_limited", "message": "Too many requests. One moment."}

        session_id = uuid.uuid4().hex
        self.sessions.store.open_chat_session(session_id, phone)
        company = self.sessions.rules.get("company") or {}
        return 200, {
            "session_id": session_id,
            "greeting": f"Thanks — you're through to {company.get('name', 'us')}. "
                        f"What can we help you with?",
            # From here rather than typed into the marketing page: a number hardcoded in
            # HTML is a second source of truth nobody remembers to change, and the one
            # place it appears is the moment the chat has already failed.
            "call_us": str(company.get("phone", "")),
        }

    def chat_message(self, payload: dict[str, Any], *,
                     ip: str = "") -> tuple[int, dict[str, Any]]:
        """Take the message and answer later. Carries no number of its own.

        The number comes from the session, so a caller cannot change whose history they are
        reading by editing one field of the next request.
        """
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return 400, {"error": "session_id is required"}

        phone = self.sessions.store.chat_session_phone(session_id)
        if not phone:
            # Also what a widget left open across a database change sees. Saying so plainly
            # lets the front end open a fresh session instead of showing a dead box.
            return 404, {"error": "unknown_session",
                         "message": "This chat has expired. Start a new one."}

        text = str(payload.get("text") or "").strip()
        if not text:
            return 400, {"error": "text is required"}
        if len(text) > MAX_MESSAGE_CHARS:
            return 413, {"error": "too_long", "limit": MAX_MESSAGE_CHARS}
        if not self.per_session.allow(session_id) or not self.per_ip.allow(ip or session_id):
            return 429, {"error": "rate_limited",
                         "message": "Too many messages. One moment."}

        with self._lock:
            running = self._pending.get(session_id)
            if running is not None and not running.done:
                # One turn at a time per session. The conversation holds a message list and
                # two workers appending to it would interleave one sentence into another.
                return 409, {"error": "still_working",
                             "message": "Still working on your last message — one moment."}
            work = _Pending()
            self._pending[session_id] = work

        threading.Thread(target=self._run_turn,
                         args=(session_id, phone, text, work), daemon=True).start()
        return 202, {"status": "working", "session_id": session_id}

    def _run_turn(self, session_id: str, phone: str, text: str,
                  work: _Pending) -> None:
        try:
            talk = self.sessions.get(session_id, phone=phone)
            turn = talk.say(text)
            for step in turn.steps:
                for tool in step.tools:
                    work.note_tool(tool)
            work.reply = turn.reply
            work.closed = talk.finished
            # Saved before the reply is collected, not after. If this process stops between
            # the two the customer resends one sentence; the other order loses the turn.
            self.sessions.save(session_id, talk, phone=phone)
            self.sessions.store.add_message(channel="chat", speaker="customer", text=text,
                                            phone=phone, session_id=session_id)
            self.sessions.store.add_message(channel="chat", speaker="agent",
                                            text=turn.reply, phone=phone,
                                            session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            # The customer gets an apology, not a stack trace, and the type is kept so
            # there is something to go on. A worker that dies silently leaves the front end
            # polling for ever.
            work.error = type(exc).__name__
        finally:
            work.done = True

    def chat_poll(self, payload: dict[str, Any], *,
                  ip: str = "") -> tuple[int, dict[str, Any]]:
        """Is the reply ready? Cheap, and deliberately **not** rate-limited.

        Polling is not the customer talking. Throttling it would only make the widget look
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
            # Handed over once and then forgotten, or two polls crossing in flight deliver
            # the same reply twice.
            del self._pending[session_id]

        if work.error:
            return 200, {"status": "error",
                         "message": "Sorry — something went wrong at our end. Please try "
                                    "again.",
                         "detail": work.error}
        return 200, {"status": "ready", "reply": work.reply or "", "closed": work.closed}


# The three ids the widget needs, and nothing else. Deliberately plain: this is a place to
# type into, not a design. The business's own page supplies the markup and the styling.
_TRY_PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chat — local test</title>
<style>
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 40rem; margin: 2rem auto;
         padding: 0 1rem; }
  #chat-log { border: 1px solid #ccc; border-radius: .5rem; padding: .75rem;
              height: 26rem; overflow-y: auto; }
  #chat-log p { margin: 0 0 .6rem; }
  #chat-log .agent { color: #0a4; }
  #chat-log .customer { color: #333; text-align: right; }
  form { display: flex; gap: .5rem; margin-top: .75rem; }
  #chat-input { flex: 1; padding: .5rem; }
  small { color: #888; }
</style>
<h1>Chat</h1>
<small>Local test page. Nothing here is the customer-facing site.</small>
<div id="chat-log"></div>
<form onsubmit="event.preventDefault()">
  <input id="chat-input" autocomplete="off" placeholder="Your phone number to start">
  <button id="chat-send" type="submit">Send</button>
</form>
<script src="/chat/widget.js"></script>
"""


def make_handler(inbound: Inbound, origins: list[str]) -> type[BaseHTTPRequestHandler]:
    """`origins` may call these endpoints from a browser. Exact origins, never `*`.

    Resolved once here rather than per request: an allow-list that can change under a
    running server is one more thing that behaves differently at 3am than it did in a test.
    """
    allowed_origins = frozenset(origins)

    class Handler(BaseHTTPRequestHandler):
        server_version = "BatInbound/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _client_ip(self) -> str:
            # Behind nginx the socket address is always the proxy, so prefer the header it
            # sets. Only trusted because nothing else can reach this port.
            return (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() \
                or self.client_address[0]

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _allow_origin(self) -> str:
            """The Origin header echoed back if it is one we allow, else nothing.

            Echoed rather than answered with `*`: these endpoints spend money on a model
            call and carry a session id, so any page on the internet being able to run up
            the bill and read the replies is not a trade worth making.
            """
            origin = self.headers.get("Origin", "")
            return origin if origin and origin in allowed_origins else ""

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if (allowed := self._allow_origin()):
                self.send_header("Access-Control-Allow-Origin", allowed)
                # Caches must not serve one origin's answer to another.
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            """The preflight. Without it `BaseHTTPRequestHandler` answers 501 and the
            browser never sends the real request — which is what was happening, because
            posting JSON is not a simple request and every message was preflighted."""
            allowed = self._allow_origin()
            if not allowed:
                self._send(403, b"", "text/plain")
                return
            self.send_response(204)
            for header, value in (("Access-Control-Allow-Origin", allowed),
                                  ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                                  ("Access-Control-Allow-Headers", "Content-Type"),
                                  ("Access-Control-Max-Age", "86400"),
                                  ("Vary", "Origin"), ("Content-Length", "0")):
                self.send_header(header, value)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            route = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
            if route == "/health":
                self._send(200, b'{"ok":true}', "application/json")
            elif route in ("/", "/chat"):
                # A page to try the widget on, and nothing more. The real page is the
                # business's own site; this exists so that "allow localhost" has
                # something to open, and so the first person to test a change is not
                # doing it against a live marketing page.
                self._send(200, _TRY_PAGE.encode(), "text/html; charset=utf-8")
            elif route == "/chat/widget.js":
                # Served from here rather than copied into the marketing page, so the chat
                # can be fixed without touching the site and the version customers run is
                # the one in git.
                try:
                    body = (Path(__file__).parent / "static" / "widget.js").read_bytes()
                except OSError:
                    self._send(404, b"not found", "text/plain")
                    return
                self._send(200, body, "application/javascript; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            # Matched exactly, with any query string removed. Prefix matching would send
            # /chat/new to the handler for /chat/message.
            route = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
            if route not in _ROUTES:
                self._send(404, b"not found", "text/plain")
                return
            try:
                payload = json.loads(self._body() or b"{}")
                handler = getattr(inbound, _ROUTES[route])
                code, data = handler(payload, ip=self._client_ip())
            except Exception as exc:  # noqa: BLE001
                # A stack trace must never reach a customer.
                code, data = 200, {"error": "internal", "detail": type(exc).__name__}
            self._send(code, json.dumps(data).encode(), "application/json")

    return Handler


def serve(project: str, database: str, *, port: int = 8770,
          origins: tuple[str, ...] = (), supervisor: str = "") -> None:
    from bat.live.integrations import live_status

    sessions = Sessions(project, database, supervisor=supervisor)
    handler = make_handler(Inbound(sessions), list(origins))
    status = live_status()
    print(f"{project} on http://127.0.0.1:{port}  db={database}")
    # Printed at every start, in front of whoever is starting it. A screen that says a tool
    # is mocked while the process is sending real texts is the failure this line exists to
    # prevent — somebody reads it, believes nothing is going out, and stops checking.
    print(f"live: {status['effectively_live'] or 'nothing — everything is simulated'}")
    if origins:
        print(f"browsers allowed from: {', '.join(origins)}")
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


if __name__ == "__main__":
    import argparse

    parse = argparse.ArgumentParser(description=__doc__)
    parse.add_argument("--project", default="plumbing")
    parse.add_argument("--db", default="runs/live.db")
    parse.add_argument("--port", type=int, default=8770)
    parse.add_argument("--origin", action="append", default=[],
                       help="an exact origin a browser may call from; repeatable. "
                            "Defaults to this machine only — a public origin is a "
                            "decision somebody types out.")
    parse.add_argument("--supervisor", default="",
                       help="Telegram chat id an escalation goes to")
    args = parse.parse_args()
    # This machine, unless somebody says otherwise. A default that let any page on the
    # internet spend money on model calls is not a default.
    origins = tuple(args.origin) or (f"http://127.0.0.1:{args.port}",
                                     f"http://localhost:{args.port}")
    serve(args.project, args.db, port=args.port, origins=origins,
          supervisor=args.supervisor)

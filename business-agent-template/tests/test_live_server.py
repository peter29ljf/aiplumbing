"""The three endpoints a customer's browser actually talks to.

`Inbound` has no HTTP in it — every method returns `(status, payload)` — so all of this
runs without a socket, without a model and without a network. The LLM is a script.

What is worth checking here is not the routing. It is the handful of decisions that were
each paid for once:

- **the reply does not come back on the request that sent the message.** The first turn was
  measured at 129 seconds and Cloudflare cuts an idle connection at 100.
- **a number is required before anything is spent.** Not because the model needs it — it
  asks anyway — but because history, warranty and booking all hang off it, and the gate has
  to be code rather than a rule the model can be talked out of.
- **one turn at a time per session**, or two workers append to one message list.
- **poll is not rate-limited**, or the widget looks broken while the agent works fine.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.live.integrations import gate  # noqa: E402
from bat.live.server import Inbound, valid_phone  # noqa: E402
from bat.live.sessions import Sessions  # noqa: E402

PHONE = "604-721-8629"


class Scripted:
    """An LLM that says one thing. Enough to drive a turn; costs nothing."""

    def __init__(self, said: str = "Thanks — what's gone wrong?") -> None:
        self.said, self.asked = said, 0

    def chat(self, role: str, messages: list[dict], tools: Any = None) -> Any:
        from types import SimpleNamespace

        self.asked += 1
        return SimpleNamespace(content=self.said, tool_calls=[])

    def limit(self, name: str, default: Any) -> Any:
        return default


@pytest.fixture(autouse=True)
def nothing_live(monkeypatch):
    monkeypatch.setenv(gate.ENV_MASTER, "false")
    monkeypatch.setenv(gate.ENV_TOOLS, "")


@pytest.fixture()
def inbound(tmp_path: Path) -> Inbound:
    return Inbound(Sessions("plumbing", tmp_path / "live.db", llm=Scripted()))


def _settle(inbound: Inbound, session_id: str, tries: int = 200) -> dict:
    """Poll the way the widget does, rather than sleeping a fixed amount."""
    for _ in range(tries):
        code, answer = inbound.chat_poll({"session_id": session_id})
        if answer.get("status") in ("ready", "error", "idle"):
            return answer
        time.sleep(0.01)
    raise AssertionError("the turn never finished")


# ---- the number, before anything is spent -------------------------------


def test_a_chat_cannot_be_opened_without_a_number(inbound: Inbound):
    code, answer = inbound.chat_new({"phone": ""})

    assert code == 400
    assert answer["error"] == "phone_required"


def test_the_number_is_checked_before_a_session_exists(inbound: Inbound):
    """Before, deliberately. A session opened and then abandoned is a row nobody reads."""
    inbound.chat_new({"phone": "hello"})

    assert inbound.sessions.store.chat_session_phone("") == ""


@pytest.mark.parametrize("number", ["604-721-8629", "+1 (604) 721-8629", "6047218629",
                                    "16047218629"])
def test_the_ways_somebody_writes_their_own_number(number: str):
    assert valid_phone(number)


@pytest.mark.parametrize("number", ["", "12345", "not a number", "604-721-862"])
def test_what_is_not_a_number(number: str):
    assert not valid_phone(number)


def test_opening_a_chat_costs_no_model_call(inbound: Inbound):
    """Somebody who opens the widget and wanders off should cost nothing."""
    inbound.chat_new({"phone": PHONE})

    assert inbound.sessions._llm.asked == 0


# ---- the reply does not come back on the same request -------------------


def test_a_message_is_accepted_and_answered_later(inbound: Inbound):
    """The 129-second turn against Cloudflare's 100-second cut."""
    _, opened = inbound.chat_new({"phone": PHONE})

    code, answer = inbound.chat_message({"session_id": opened["session_id"],
                                         "text": "my tap is dripping"})

    assert code == 202
    assert answer["status"] == "working"
    assert "reply" not in answer

    assert _settle(inbound, opened["session_id"])["status"] == "ready"


def test_the_reply_arrives_on_the_poll(inbound: Inbound):
    _, opened = inbound.chat_new({"phone": PHONE})
    inbound.chat_message({"session_id": opened["session_id"], "text": "dripping tap"})

    assert _settle(inbound, opened["session_id"])["reply"]


def test_a_reply_is_handed_over_once(inbound: Inbound):
    """Two polls crossing in flight must not deliver the same message twice."""
    _, opened = inbound.chat_new({"phone": PHONE})
    inbound.chat_message({"session_id": opened["session_id"], "text": "dripping tap"})
    _settle(inbound, opened["session_id"])

    _, again = inbound.chat_poll({"session_id": opened["session_id"]})
    assert again["status"] == "idle"


def test_the_customer_is_told_what_is_happening_while_they_wait(inbound: Inbound):
    """A minute of three dots looks the same as a system that has died."""
    from bat.live.server import DOING, _doing

    assert _doing("calendar.find_slots") == DOING["calendar.find_slots"]
    assert _doing("calendar_find_slots") == DOING["calendar.find_slots"], (
        "the engine reports the wire spelling; keying one way and reading the other is "
        "how every customer came to see the fallback"
    )


def test_a_tool_with_no_line_does_not_wipe_the_one_before_it():
    """Almost every batch ends on a bookkeeping call. Returning the fallback for those
    meant the fallback was all anybody ever saw."""
    from bat.live.server import _Pending

    work = _Pending()
    work.note_tool("calendar.find_slots")
    work.note_tool("ticket.set_fields")

    assert work.doing == "Checking the calendar"


# ---- one turn at a time -------------------------------------------------


def test_a_second_message_while_one_is_running_is_refused(inbound: Inbound,
                                                          monkeypatch):
    """Two workers appending to one message list interleave one customer's sentence into
    the middle of another's."""
    _, opened = inbound.chat_new({"phone": PHONE})
    session_id = opened["session_id"]

    slow = inbound.sessions._llm
    original = slow.chat
    monkeypatch.setattr(slow, "chat",
                        lambda *a, **k: (time.sleep(0.3), original(*a, **k))[1])

    inbound.chat_message({"session_id": session_id, "text": "first"})
    code, answer = inbound.chat_message({"session_id": session_id, "text": "second"})

    assert code == 409
    assert answer["error"] == "still_working"
    _settle(inbound, session_id, tries=500)


# ---- the caps that stop a public endpoint being a bill -------------------


def test_a_very_long_message_is_refused(inbound: Inbound):
    _, opened = inbound.chat_new({"phone": PHONE})

    code, answer = inbound.chat_message({"session_id": opened["session_id"],
                                         "text": "x" * 5000})

    assert code == 413
    assert answer["limit"] == 1000


def test_too_many_messages_in_a_minute_are_refused(inbound: Inbound):
    _, opened = inbound.chat_new({"phone": PHONE})
    session_id = opened["session_id"]

    codes = [inbound.chat_message({"session_id": session_id, "text": "hi"})[0]
             for _ in range(20)]

    assert 429 in codes


def test_polling_is_not_rate_limited(inbound: Inbound):
    """It is not the customer talking. Throttling it makes the widget look broken while
    the agent is working perfectly well."""
    _, opened = inbound.chat_new({"phone": PHONE})

    codes = {inbound.chat_poll({"session_id": opened["session_id"]})[0]
             for _ in range(60)}

    assert codes == {200}


# ---- a session nobody has heard of --------------------------------------


def test_a_message_on_an_unknown_session_says_to_start_again(inbound: Inbound):
    """What a widget left open across a database change sees. Saying so plainly lets the
    front end open a fresh session instead of showing a dead box."""
    code, answer = inbound.chat_message({"session_id": "made up", "text": "hello"})

    assert code == 404
    assert answer["error"] == "unknown_session"


# ---- what survives a restart --------------------------------------------


def test_a_conversation_is_still_there_after_the_process_that_held_it_is_gone(
        tmp_path: Path):
    """The whole point of the store. Somebody who has given their name, their address and
    their fault must not be asked for all of it again because of a deploy."""
    db = tmp_path / "live.db"
    first = Inbound(Sessions("plumbing", db, llm=Scripted()))
    _, opened = first.chat_new({"phone": PHONE})
    session_id = opened["session_id"]
    first.chat_message({"session_id": session_id, "text": "my tap is dripping"})
    _settle(first, session_id)

    # A different process: nothing in memory, the same database.
    second = Inbound(Sessions("plumbing", db, llm=Scripted()))
    second.chat_message({"session_id": session_id, "text": "it's the kitchen one"})

    assert _settle(second, session_id)["status"] == "ready"
    assert second.sessions.get(session_id).tags.get("phone") == PHONE, (
        "the number the customer typed into the form did not survive"
    )

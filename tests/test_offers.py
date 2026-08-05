"""Offering a job to the technician: accept in one tap, decline with a reason."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.live import offers as offers_mod  # noqa: E402
from plumbing.live.offers import Offers  # noqa: E402
from plumbing.live.server import Inbound  # noqa: E402
from plumbing.store import SqliteStore  # noqa: E402


@pytest.fixture()
def offers(tmp_path: Path) -> Offers:
    return Offers(SqliteStore(tmp_path / "t.db"))


class Sessions:
    def __init__(self, offers: Offers):
        self.offers = offers
        self.store = offers.store
        self.recorded: list[dict] = []

    def technician_by_chat_id(self, chat_id):
        return {"id": "t_wang"} if chat_id == "555" else None

    def record_technician_message(self, *, chat_id, text):
        self.recorded.append({"chat_id": chat_id, "text": text})

    def get(self, **_kw):
        raise AssertionError("a technician message must not start a customer conversation")


@pytest.fixture()
def bot(monkeypatch, offers: Offers):
    import plumbing.integrations.telegram as tg

    monkeypatch.setattr(tg, "verify_webhook_secret", lambda provided: None)
    inbound = Inbound(Sessions(offers))
    inbound.said: list[str] = []
    inbound._telegram_reply = lambda chat_id, text: inbound.said.append(text)
    return inbound


def _offer(offers: Offers) -> str:
    return offers.create(ticket_id="TK-0001", chat_id="555",
                         summary="Wed 9:00 am — dripping tap, Surrey").offer_id


def _tap(bot, offer_id, decision, chat="555"):
    return bot.telegram(
        {"callback_query": {"id": "cb1", "data": offers_mod.callback_data(offer_id, decision),
                            "message": {"chat": {"id": chat}}}},
        secret="ok",
    )


def _type(bot, text, chat="555"):
    return bot.telegram(
        {"message": {"text": text, "chat": {"id": chat}, "from": {"id": "u1"}}}, secret="ok"
    )


# ---- accepting -------------------------------------------------------


def test_accepting_is_one_tap(bot, offers: Offers):
    """Nothing else is asked. The technician is usually holding something."""
    offer_id = _offer(offers)
    code, _ = _tap(bot, offer_id, offers_mod.ACCEPT)
    assert code == 200
    assert offers.get(offer_id).state == "accepted"
    assert bot.said == []                      # no follow-up question


# ---- declining, both ways --------------------------------------------


def test_typing_the_reason_first_then_tapping_decline(bot, offers: Offers):
    """The flow the message asks for: say why, then press the button."""
    offer_id = _offer(offers)
    _type(bot, "already on a boiler in Burnaby")
    _tap(bot, offer_id, offers_mod.DECLINE)

    settled = offers.get(offer_id)
    assert settled.state == "declined"
    assert settled.reason == "already on a boiler in Burnaby"
    assert bot.said == []                      # it did not need to ask


def test_tapping_decline_first_gets_asked_why(bot, offers: Offers):
    """Pressing the obvious button before reading is the interface's fault, not theirs."""
    offer_id = _offer(offers)
    _tap(bot, offer_id, offers_mod.DECLINE)

    assert offers.get(offer_id).state == "awaiting_reason"
    assert "what should I tell the customer" in bot.said[0]

    _type(bot, "van is in the shop until Thursday")
    settled = offers.get(offer_id)
    assert settled.state == "declined"
    assert settled.reason == "van is in the shop until Thursday"


def test_a_decline_is_never_left_without_a_reason(bot, offers: Offers):
    """"No" on its own leaves the office with a customer and nothing to tell them."""
    offer_id = _offer(offers)
    _tap(bot, offer_id, offers_mod.DECLINE)
    assert offers.get(offer_id).state != "declined"     # not settled until they say why


# ---- pressing twice, and old offers ----------------------------------


def test_a_settled_offer_cannot_be_answered_again(bot, offers: Offers):
    """Somebody scrolling back must not accept a job that was already declined."""
    offer_id = _offer(offers)
    _tap(bot, offer_id, offers_mod.ACCEPT)
    _tap(bot, offer_id, offers_mod.DECLINE)
    assert offers.get(offer_id).state == "accepted"


def test_a_stranger_cannot_answer_an_offer(bot, offers: Offers):
    offer_id = _offer(offers)
    _tap(bot, offer_id, offers_mod.ACCEPT, chat="999")
    assert offers.get(offer_id).state == "sent"


def test_a_meaningless_callback_is_ignored_quietly(bot, offers: Offers):
    code, _ = bot.telegram(
        {"callback_query": {"id": "cb1", "data": "nonsense", "message": {"chat": {"id": "555"}}}},
        secret="ok",
    )
    assert code == 200


# ---- the button payload ----------------------------------------------


def test_callback_data_stays_inside_telegrams_limit(offers: Offers):
    """Telegram caps it at 64 bytes and rejects the whole message when it is over."""
    offer_id = _offer(offers)
    for row in offers_mod.buttons(offer_id):
        for button in row:
            assert len(button["data"].encode()) <= 64


def test_the_settled_message_shows_the_outcome(offers: Offers):
    offer_id = _offer(offers)
    offers.decline(offer_id, "already booked")
    text = offers_mod.settled_text(offers.get(offer_id))
    assert "declined" in text and "already booked" in text


def test_offers_survive_a_restart(tmp_path: Path):
    """An offer a restart forgets is a job nobody is going to."""
    path = tmp_path / "t.db"
    offer_id = Offers(SqliteStore(path)).create(
        ticket_id="TK-0001", chat_id="555", summary="x").offer_id
    assert Offers(SqliteStore(path)).get(offer_id).state == "sent"

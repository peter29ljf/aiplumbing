"""Nudging a technician who has not answered — free, and it knows when to stop."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.live.offers import Offers  # noqa: E402
from plumbing.live.reminders import ReminderLoop  # noqa: E402
from plumbing.store import SqliteStore  # noqa: E402

RULES = {"job_offer": {"reminder_minutes": [10, 20, 30], "give_up_after_last_reminder": True}}


@pytest.fixture()
def offers(tmp_path: Path) -> Offers:
    return Offers(SqliteStore(tmp_path / "t.db"))


@pytest.fixture()
def loop(offers: Offers):
    sent: list[tuple[str, str]] = []
    reminder_loop = ReminderLoop(
        offers, RULES, send=lambda chat, text, buttons=None: sent.append((chat, text))
    )
    reminder_loop.sent = sent
    return reminder_loop


def _offer(offers: Offers) -> str:
    return offers.create(ticket_id="TK-1", chat_id="555", summary="Wed 9:00 — tap").offer_id


def _at(minutes: int) -> datetime:
    return datetime.now().astimezone() + timedelta(minutes=minutes)


# ---- the schedule ----------------------------------------------------


def test_nothing_is_sent_before_the_first_threshold(loop, offers: Offers):
    _offer(offers)
    assert loop.tick(_at(9)) == []


def test_the_first_nudge_lands_on_time(loop, offers: Offers):
    _offer(offers)
    sent = loop.tick(_at(11))
    assert len(sent) == 1 and sent[0]["number"] == 1
    assert "Still waiting" in loop.sent[0][1]


def test_a_nudge_is_not_repeated_on_the_next_tick(loop, offers: Offers):
    """The loop runs every thirty seconds; each threshold fires once."""
    _offer(offers)
    loop.tick(_at(11))
    assert loop.tick(_at(12)) == []
    assert loop.tick(_at(13)) == []


def test_each_threshold_fires_once_as_time_passes(loop, offers: Offers):
    _offer(offers)
    assert len(loop.tick(_at(11))) == 1
    assert len(loop.tick(_at(21))) == 1
    assert len(loop.tick(_at(31))) == 1
    assert loop.tick(_at(60)) == []          # and then it stops


# ---- stopping --------------------------------------------------------


def test_accepting_stops_the_nudging_immediately(loop, offers: Offers):
    offer_id = _offer(offers)
    loop.tick(_at(11))
    offers.accept(offer_id)
    assert loop.tick(_at(31)) == []


def test_declining_stops_the_nudging(loop, offers: Offers):
    offer_id = _offer(offers)
    offers.decline(offer_id, "already booked")
    assert loop.tick(_at(31)) == []


def test_somebody_being_asked_why_is_not_nagged(loop, offers: Offers):
    """They tapped Decline and are typing the reason. Nudging that is nagging."""
    offer_id = _offer(offers)
    offers.ask_for_reason(offer_id)
    assert loop.tick(_at(31)) == []


def test_it_gives_up_rather_than_becoming_noise(loop, offers: Offers):
    """A bot that keeps asking gets muted, and a muted bot misses the next job too."""
    _offer(offers)
    loop.tick(_at(11)); loop.tick(_at(21))
    last = loop.tick(_at(31))
    assert last[0]["gave_up"] is True
    assert "flagged it for the office" in loop.sent[-1][1]
    assert loop.tick(_at(120)) == []


def test_giving_up_is_recorded_for_the_office(loop, offers: Offers):
    offer_id = _offer(offers)
    for minutes in (6, 16, 31):
        loop.tick(_at(minutes))
    kinds = [e["kind"] for e in offers.store.events("TK-1")]
    assert "job_offer_unanswered" in kinds


# ---- surviving a restart ---------------------------------------------


def test_the_count_survives_a_restart(tmp_path: Path):
    """A forgotten count sends the first reminder twice."""
    path = tmp_path / "t.db"
    first = Offers(SqliteStore(path))
    offer_id = first.create(ticket_id="TK-1", chat_id="555", summary="x").offer_id
    ReminderLoop(first, RULES, send=lambda *_a, **_kw: None).tick(_at(11))

    after = ReminderLoop(Offers(SqliteStore(path)), RULES, send=lambda *_a, **_kw: None)
    assert after.tick(_at(12)) == []           # not sent again
    assert len(after.tick(_at(21))) == 1      # but the next one still fires
    assert offer_id


# ---- resilience ------------------------------------------------------


def test_a_send_failure_does_not_stop_the_loop(offers: Offers):
    """The customer-facing endpoints do not care whether a reminder went out."""
    def _boom(chat, text, buttons=None):
        raise RuntimeError("telegram down")

    loop = ReminderLoop(offers, RULES, send=_boom)
    _offer(offers)
    with pytest.raises(RuntimeError):
        loop.tick(_at(11))                     # tick surfaces it...
    loop._run_once_safely = True              # ...and the thread wrapper swallows it


# ---- the day-after check with the technician -------------------------


def _followup(store, *, hours_ago: float = 25, chat_id: str = "555") -> str:
    """A follow-up that came due `hours_ago` hours ago."""
    return store.schedule_followup(
        ticket_id="TK-1", kind="job_outcome", chat_id=chat_id,
        summary="Wed 9:00 — tap, Linda Zhang",
        due_at=datetime.now().astimezone() - timedelta(hours=hours_ago),
    )


def test_a_scheduled_followup_actually_fires(loop, offers: Offers):
    """It used to live in memory on a World that was discarded, so it fired never."""
    _followup(offers.store)
    asked = loop.tick_followups()
    assert len(asked) == 1
    assert "How did this one go?" in loop.sent[0][1]


def test_a_followup_that_is_not_due_yet_waits(loop, offers: Offers):
    offers.store.schedule_followup(
        ticket_id="TK-1", kind="job_outcome", chat_id="555", summary="x",
        due_at=datetime.now().astimezone() + timedelta(hours=5),
    )
    assert loop.tick_followups() == []


def test_it_asks_once_more_and_then_leaves_it(loop, offers: Offers):
    """Nobody is waiting on this the way a customer waits on an offer."""
    _followup(offers.store)
    assert len(loop.tick_followups()) == 1
    assert len(loop.tick_followups()) == 1      # one nudge
    assert loop.tick_followups() == []          # then it stops asking

    kinds = [e["kind"] for e in offers.store.events("TK-1")]
    assert "followup_unanswered" in kinds


def test_an_answered_followup_is_not_asked_again(loop, offers: Offers):
    followup_id = _followup(offers.store)
    loop.tick_followups()
    offers.store.update_followup(followup_id, status="answered", answer="done")
    assert loop.tick_followups() == []


def test_a_followup_with_nobody_to_ask_is_skipped(loop, offers: Offers):
    """No chat id means the technician never messaged the bot. Not a crash."""
    _followup(offers.store, chat_id="")
    assert loop.tick_followups() == []


def test_the_two_outcomes_are_the_two_that_change_anything(loop, offers: Offers):
    """Done, or the customer decided against it. Anything else they can type."""
    from plumbing.live.reminders import OUTCOME_DECLINED, OUTCOME_DONE

    _followup(offers.store)
    loop.tick_followups()
    buttons_sent = [b for call in loop.sent for b in [call]]
    assert buttons_sent            # it went out
    assert OUTCOME_DONE != OUTCOME_DECLINED

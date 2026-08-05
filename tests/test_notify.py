"""Reaching the one human on duty: message first, then make the phone ring."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.live import notify  # noqa: E402
from plumbing.integrations.gate import LiveToolUnavailable  # noqa: E402


def _args(**over):
    base = {"chat_id": "555", "phone": "+16045550201",
            "subject": "Escalation on TK-0001", "body": "Customer wants a warranty claim."}
    return {**base, **over}


def test_nothing_leaves_the_process_while_the_gate_is_shut():
    """The default. Both legs report themselves as simulated rather than pretending."""
    outcome = notify.notify_technician(**_args())
    assert outcome["telegram"]["simulated"] is True
    assert outcome["call"]["simulated"] is True
    assert outcome["errors"] == []


def test_the_details_go_to_telegram_and_the_call_says_only_one_line(monkeypatch):
    """Reading an address down a phone to someone holding a wrench is how it gets lost."""
    sent, called = {}, {}
    monkeypatch.setattr(notify, "is_live", lambda name: True)
    import plumbing.integrations.telegram as tg
    import plumbing.integrations.twilio_voice as voice

    monkeypatch.setattr(tg, "send_message",
                        lambda chat_id, text: sent.update(chat_id=chat_id, text=text) or {"message_id": "1"})
    monkeypatch.setattr(voice, "say_and_hang_up",
                        lambda to, text: called.update(to=to, text=text) or {"call_id": "c1"})

    notify.notify_technician(**_args())

    assert "Customer wants a warranty claim." in sent["text"]
    assert called["text"] == notify.CALL_SCRIPT
    assert "TK-0001" not in called["text"]      # the call carries no detail, on purpose


def test_a_dead_telegram_still_makes_the_phone_ring(monkeypatch):
    """They cannot read the job yet, but they still need to know something is waiting."""
    monkeypatch.setattr(notify, "is_live", lambda name: True)
    import plumbing.integrations.telegram as tg
    import plumbing.integrations.twilio_voice as voice

    def _down(chat_id, text):
        raise LiveToolUnavailable("telegram is down")

    monkeypatch.setattr(tg, "send_message", _down)
    monkeypatch.setattr(voice, "say_and_hang_up", lambda to, text: {"call_id": "c1"})

    outcome = notify.notify_technician(**_args())
    assert outcome["call"]["call_id"] == "c1"
    assert any("telegram" in e for e in outcome["errors"])


def test_a_failed_call_does_not_lose_the_message(monkeypatch):
    monkeypatch.setattr(notify, "is_live", lambda name: True)
    import plumbing.integrations.telegram as tg
    import plumbing.integrations.twilio_voice as voice

    def _down(to, text):
        raise LiveToolUnavailable("no answer")

    monkeypatch.setattr(tg, "send_message", lambda chat_id, text: {"message_id": "1"})
    monkeypatch.setattr(voice, "say_and_hang_up", _down)

    outcome = notify.notify_technician(**_args())
    assert outcome["telegram"]["message_id"] == "1"
    assert any("call" in e for e in outcome["errors"])


def test_reaching_nobody_never_takes_down_the_customer_conversation(monkeypatch):
    """The customer is still sitting there. This is the office's problem, not theirs."""
    monkeypatch.setattr(notify, "is_live", lambda name: True)
    import plumbing.integrations.telegram as tg
    import plumbing.integrations.twilio_voice as voice

    def _down(*_a, **_kw):
        raise LiveToolUnavailable("everything is down")

    monkeypatch.setattr(tg, "send_message", _down)
    monkeypatch.setattr(voice, "say_and_hang_up", _down)

    outcome = notify.notify_technician(**_args())      # must not raise
    assert len(outcome["errors"]) == 2


def test_a_non_urgent_note_does_not_ring_anybody(monkeypatch):
    monkeypatch.setattr(notify, "is_live", lambda name: True)
    import plumbing.integrations.telegram as tg

    monkeypatch.setattr(tg, "send_message", lambda chat_id, text: {"message_id": "1"})
    outcome = notify.notify_technician(**_args(), urgent=False)
    assert outcome["call"] is None


def test_an_escalation_reaches_the_on_duty_technician():
    """End to end through the tool the agents actually call."""
    from plumbing.tools.comms_tools import escalate_raise
    from plumbing.tools.registry import ToolContext
    from plumbing.world import World

    world = World("2026-08-05T10:00:00-07:00")
    ticket = world.create_ticket("+16047218629")
    world.transition_ticket(ticket.ticket_id, "Phone Verified")

    result = escalate_raise(ToolContext(world=world, agent_name="intake"),
                            ticket_id=ticket.ticket_id, reason="warranty claim",
                            details="Customer says last year's repair failed.")
    assert result
    notified = world.escalations[-1]["notified"]
    assert notified["telegram"]["simulated"] is True
    assert "warranty claim" in notified["telegram"]["text"]

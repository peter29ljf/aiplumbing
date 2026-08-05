"""Reaching the one human on duty. Telegram, and nothing else.

It used to ring them as well, on the theory that a message can be missed. The deployment
chose not to pay for that, and a second channel that half works is worse than one that
either works or says plainly that it did not — the roster's numbers were fictional, the
calls went nowhere, and nothing anywhere said so.
"""

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
    """The default. It reports itself as simulated rather than pretending."""
    outcome = notify.notify_technician(**_args())
    assert outcome["telegram"]["simulated"] is True
    assert outcome["call"] is None
    assert outcome["errors"] == []





def test_the_details_go_to_telegram(monkeypatch):
    """Everything the technician needs, in writing. Reading an address down a phone to
    somebody holding a wrench is how it gets lost."""
    sent = {}
    monkeypatch.setattr(notify, "is_live", lambda name: name == "telegram.send")
    import plumbing.integrations.telegram as tg

    monkeypatch.setattr(tg, "send_message",
                        lambda chat_id, text: sent.update(chat_id=chat_id, text=text)
                        or {"message_id": "1"})

    outcome = notify.notify_technician(**_args())

    assert sent["chat_id"] == "555"
    assert "TK-0001" in sent["text"] and "warranty claim" in sent["text"]
    assert outcome["errors"] == []


def test_nobody_is_rung_however_urgent_it_is(monkeypatch):
    monkeypatch.setattr(notify, "is_live", lambda name: True)
    import plumbing.integrations.telegram as tg

    monkeypatch.setattr(tg, "send_message", lambda chat_id, text: {"message_id": "1"})

    for urgent in (True, False):
        assert notify.notify_technician(**_args(), urgent=urgent)["call"] is None


def test_reaching_nobody_never_takes_down_the_customer_conversation(monkeypatch):
    """The customer is still sitting there. This is the office's problem, not theirs."""
    monkeypatch.setattr(notify, "is_live", lambda name: True)
    import plumbing.integrations.telegram as tg

    def _down(*_a, **_kw):
        raise LiveToolUnavailable("everything is down")

    monkeypatch.setattr(tg, "send_message", _down)

    outcome = notify.notify_technician(**_args())      # must not raise

    assert outcome["errors"] == ["telegram: everything is down"]
    assert outcome["telegram"] is None



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

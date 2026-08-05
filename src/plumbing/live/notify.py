"""Getting a person's attention, and then getting out of their way.

There is one technician on duty, and they are also the supervisor. Everything the office
needs from a human goes to them, in two steps:

1. **The details go to Telegram.** Free, threaded, and it can hold an address and a job
   description without arriving as three separate texts.
2. **Then the phone rings**, and the call says one sentence — *there is a new job, please
   check your messages* — and hangs up.

The call carries no information on purpose. A phone is what gets somebody off a ladder;
Telegram is what they can read, scroll back through and reply to. Reading a job address
down a phone line to someone holding a wrench is how the address gets written on a wrist
and then washed off.

The ringing is the escalation, not the message. If Telegram is down the call still goes
out, because a technician who cannot see the details still needs to know something is
waiting; if the call fails the message is still there. Neither failure is allowed to
swallow the other.
"""

from __future__ import annotations

from typing import Any

from plumbing.integrations import is_live
from plumbing.integrations.gate import LiveToolUnavailable

# What the call says, every time. Not generated: a message that varies is a message that
# can go wrong, and this one only has to make a phone ring in someone's pocket.


def offer_job(
    *,
    offers: Any,
    ticket_id: str,
    chat_id: str,
    phone: str,
    summary: str,
) -> dict[str, Any]:
    """Put a job in front of the technician with an Accept and a Decline under it.

    The buttons exist so an answer costs one tap. Everything about how the answer comes
    back lives in `plumbing.live.offers`; this only sends it and makes the phone ring.
    """
    from plumbing.live import offers as offers_mod  # noqa: PLC0415

    offer = offers.create(ticket_id=ticket_id, chat_id=chat_id, summary=summary)
    text = offers_mod.offer_text(summary)
    outcome: dict[str, Any] = {"offer_id": offer.offer_id, "telegram": None,
                               "call": None, "errors": []}

    if is_live("telegram.send"):
        from plumbing.integrations import telegram  # noqa: PLC0415

        try:
            sent = telegram.send_message(chat_id, text, buttons=offers_mod.buttons(offer.offer_id))
            offers.set_message_id(offer.offer_id, sent["message_id"])
            outcome["telegram"] = sent
        except LiveToolUnavailable as exc:
            outcome["errors"].append(f"telegram: {exc}")
    else:
        outcome["telegram"] = {"simulated": True, "chat_id": chat_id, "text": text,
                               "buttons": offers_mod.buttons(offer.offer_id)}

    # No call. Technicians are reached on Telegram and nowhere else — the deployment chose
    # not to pay for a ringing phone, and a second channel that half works is worse than
    # one that either works or says it did not.
    return outcome



def notify_technician(
    *,
    chat_id: str,
    phone: str,
    subject: str,
    body: str,
    urgent: bool = True,
) -> dict[str, Any]:
    """Message first, then ring. Returns what actually happened on each leg.

    Never raises. A failure to reach a technician must not take down the conversation the
    customer is still sitting in — it has to be reported so it can be retried or shown in
    the console, not thrown at whoever happened to trigger it.
    """
    outcome: dict[str, Any] = {"telegram": None, "call": None, "errors": []}

    text = f"<b>{subject}</b>\n\n{body}"
    if is_live("telegram.send"):
        from plumbing.integrations import telegram  # noqa: PLC0415

        try:
            outcome["telegram"] = telegram.send_message(chat_id, text)
        except LiveToolUnavailable as exc:
            outcome["errors"].append(f"telegram: {exc}")
    else:
        outcome["telegram"] = {"simulated": True, "chat_id": chat_id, "text": text}

    # No call, however urgent. Technicians are reached on Telegram and nowhere else: the
    # deployment chose not to pay for a ringing phone, and a second channel that half works
    # is worse than one that either works or says plainly that it did not.
    return outcome

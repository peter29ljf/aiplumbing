#!/usr/bin/env python3
"""One real call down every leg that reaches the outside world.

This is the checklist before a real customer is put in front of the flow. Each leg is
switched on individually in `.env` or the systemd unit, and each fails in its own way —
missing credentials, a library nobody installed, a chat id the bot has never been spoken
to. Finding all of that out during a booking means finding it out while somebody is
waiting, and the failure the agent reports is "the technician could not be reached", which
says nothing about which of the four causes it was.

    python3 scripts/check_live.py                    # every leg that is switched on
    python3 scripts/check_live.py telegram email     # only these
    python3 scripts/check_live.py --all              # try them all, on or off

Nothing here is addressed to a customer. The text goes to the technician's own chat, the
email to our own inbox, the calendar entry is created and deleted again. The one that does
reach a person is the SMS, which goes to STAFF_PHONE_NUMBERS — so that leg says who it is
about to text and stops unless `--yes` is given.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing import config  # noqa: E402
from plumbing.integrations.gate import (  # noqa: E402
    LiveToolUnavailable,
    is_live,
    live_status,
    master_enabled,
)
from plumbing.paths import load_dotenv  # noqa: E402

STAMP = datetime.now().strftime("%H:%M:%S")


def check_telegram() -> str:
    """A message to the technician's own chat. Free, and the one production relies on."""
    from plumbing.integrations import telegram

    chat_id = _technician_chat_id()
    if not chat_id:
        raise LiveToolUnavailable(
            "No technician in config/world_seed.yaml has a telegram_chat_id. The id is "
            "learned when they first message the bot; run scripts/setup_telegram.py."
        )
    sent = telegram.send_message(chat_id, f"Live check at {STAMP} — ignore this.")
    return f"message {sent['message_id']} to chat {chat_id}"


def check_email() -> str:
    """An email to ourselves, at the address customers are told to send photographs to."""
    from plumbing.integrations import gmail_email

    to = os.environ.get("GMAIL_USER") or config.business_rules()["company"].get("email", "")
    if not to:
        raise LiveToolUnavailable("Neither GMAIL_USER nor company.email is set.")
    sent = gmail_email.send_email(to, f"Live check {STAMP}", "Ignore this.")
    return f"message {sent['message_id']} to {to}"


def check_calendar() -> str:
    """Read the diary, write an entry, and take it out again.

    All three, because they fail separately: a service account can be allowed to read a
    calendar and not to write to it, and finding that out at the moment of a booking means
    a customer has been told a time nobody has.
    """
    from plumbing.integrations import google_calendar

    # A week, which is what `free_slots` actually asks for. It was a year, and Google
    # refused with `timeRangeTooLong` — a check that fails where the app would succeed
    # sends somebody hunting a credential that was never missing.
    now = datetime.now().astimezone()
    busy = google_calendar.busy_periods(now, now + timedelta(days=7))
    # Far enough out that a stray entry, if the delete below ever fails, is not sitting in
    # a week somebody is booking into.
    start = now + timedelta(days=365)
    event_id = google_calendar.create_event(
        start=start, duration_minutes=30,
        summary=f"Live check {STAMP} — safe to delete",
        description="Written by scripts/check_live.py and removed immediately.",
    )
    google_calendar.delete_event(event_id)
    return f"read {len(busy)} busy period(s), wrote and removed event {event_id}"


def check_sms(confirmed: bool) -> str:
    """The only leg that reaches a person's pocket, so it asks first."""
    from plumbing.integrations import twilio_sms

    numbers = [n.strip() for n in os.environ.get("STAFF_PHONE_NUMBERS", "").split(",")
               if n.strip()]
    if not numbers:
        raise LiveToolUnavailable(
            "STAFF_PHONE_NUMBERS is empty. Set it rather than testing against a customer."
        )
    if not confirmed:
        return f"SKIPPED — would text {numbers[0]}. Re-run with --yes to send it."
    sent = twilio_sms.send_sms(numbers[0], f"Live check at {STAMP} — ignore this.")
    return f"message {sent['message_id']} to {numbers[0]}"


# The tool each leg is switched on by, so what this reports and what the agent can
# actually do are the same question. A leg that passed here while the tool it stands for
# was off would be the most misleading possible green tick.
LEGS = {
    "telegram": ("telegram.send", check_telegram),
    "email": ("email.send", check_email),
    "calendar": ("calendar.create_appointment", check_calendar),
    "sms": ("sms.send", check_sms),
}


def _technician_chat_id() -> str:
    for spec in config.world_seed().get("technicians", []):
        if spec.get("on_duty", True) and spec.get("telegram_chat_id"):
            return str(spec["telegram_chat_id"])
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("only", nargs="*", choices=[*sorted(LEGS), []],
                        help="legs to check; default is every one that is switched on")
    parser.add_argument("--all", action="store_true",
                        help="try every leg, including ones the switches have off")
    parser.add_argument("--yes", action="store_true",
                        help="actually send the SMS, which reaches somebody's phone")
    args = parser.parse_args(argv)

    load_dotenv()
    status = live_status()
    print(f"master switch : {status['master_switch']} ({status['master_switch_source']})")
    print(f"live tools    : {', '.join(status['effectively_live']) or 'nothing'} "
          f"({status['tools_source']})\n")

    if not master_enabled() and not args.all:
        print("Everything is running against the simulator, so there is nothing to check.")
        print("Set PLUMBING_LIVE_ENABLED=true and PLUMBING_LIVE_TOOLS, or pass --all.")
        return 0

    wanted = args.only or sorted(LEGS)
    failures = 0
    for name in wanted:
        tool, check = LEGS[name]
        if not args.all and not is_live(tool):
            print(f"  --   {name:<9} {tool} is not switched on")
            continue
        try:
            outcome = check(args.yes) if name == "sms" else check()
        except LiveToolUnavailable as exc:
            failures += 1
            print(f"  FAIL {name:<9} {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad leg must not hide the others
            failures += 1
            print(f"  FAIL {name:<9} {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {name:<9} {outcome}")

    print()
    if failures:
        print(f"{failures} leg(s) cannot reach their service. Do not put a customer in "
              f"front of this until they can — a failed send stops the agent confirming, "
              f"which is right, but it stops it mid-conversation.")
    else:
        print("Every leg checked is reachable.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

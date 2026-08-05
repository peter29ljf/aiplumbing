"""Outbound voice — the call that only makes a phone ring.

One use: telling a technician a job is waiting. It says a single fixed sentence and hangs
up. The details are already in Telegram, where they can be read twice and scrolled back
to; a phone call is only good for getting someone's attention.

TwiML is passed inline rather than fetched from a URL, so this needs no public callback
endpoint to place a call.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from xml.sax.saxutils import escape

from plumbing.integrations.gate import LiveToolUnavailable, require_env

_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"


def say_and_hang_up(to: str, text: str) -> dict[str, Any]:
    """Ring `to`, say `text` twice, hang up.

    Twice because the first second of a call is spent getting a phone to an ear, and a
    technician who misses the sentence has no way to ask for it again.
    """
    env = require_env("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")
    sid = env["TWILIO_ACCOUNT_SID"]

    spoken = escape(text)
    twiml = (
        f"<Response><Pause length='1'/><Say>{spoken}</Say>"
        f"<Pause length='1'/><Say>{spoken}</Say><Hangup/></Response>"
    )
    payload = urllib.parse.urlencode(
        {"To": to, "From": env["TWILIO_PHONE_NUMBER"], "Twiml": twiml}
    ).encode()

    request = urllib.request.Request(_API.format(sid=sid), data=payload, method="POST")
    token = base64.b64encode(f"{sid}:{env['TWILIO_AUTH_TOKEN']}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise LiveToolUnavailable(f"Twilio rejected the call: {exc.read()[:300]!r}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Twilio unreachable: {exc}") from exc

    return {"provider": "twilio", "call_id": data.get("sid", ""), "status": data.get("status", ""), "to": to}

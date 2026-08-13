"""Twilio SMS adapter.

Reached only through the gate. Sending a message here costs money and reaches a real
phone, so the caller is expected to have checked is_live() first.
"""

from __future__ import annotations

from typing import Any

from bat.live.integrations.gate import LiveToolUnavailable, require_env

_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def send_sms(to: str, body: str) -> dict[str, Any]:
    """Send a text message. Returns the provider's message id."""
    import urllib.error  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    env = require_env("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")
    sid = env["TWILIO_ACCOUNT_SID"]

    payload = urllib.parse.urlencode(
        {"To": to, "From": env["TWILIO_PHONE_NUMBER"], "Body": body}
    ).encode()

    request = urllib.request.Request(_API.format(sid=sid), data=payload, method="POST")
    import base64  # noqa: PLC0415

    token = base64.b64encode(f"{sid}:{env['TWILIO_AUTH_TOKEN']}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            import json  # noqa: PLC0415

            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise LiveToolUnavailable(f"Twilio rejected the message: {exc.read()[:300]!r}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Twilio unreachable: {exc}") from exc

    return {
        "provider": "twilio",
        "message_id": data.get("sid", ""),
        "status": data.get("status", ""),
        "to": data.get("to", to),
    }

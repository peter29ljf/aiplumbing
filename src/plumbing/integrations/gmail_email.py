"""Gmail adapter, using an OAuth refresh token.

Mirrors the aiphone project's approach: a refresh token is exchanged for a short-lived
access token on each send, so nothing long-lived sits in memory.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any

from plumbing.integrations.gate import LiveToolUnavailable, require_env

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def _access_token() -> str:
    env = require_env("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    payload = urllib.parse.urlencode(
        {
            "client_id": env["GMAIL_CLIENT_ID"],
            "client_secret": env["GMAIL_CLIENT_SECRET"],
            "refresh_token": env["GMAIL_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }
    ).encode()
    try:
        with urllib.request.urlopen(_TOKEN_URL, data=payload, timeout=30) as response:
            return json.loads(response.read())["access_token"]
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Could not refresh the Gmail token: {exc}") from exc


def send_email(to: str, subject: str, body: str) -> dict[str, Any]:
    env = require_env("GMAIL_USER")
    message = EmailMessage()
    message["To"] = to
    message["From"] = env["GMAIL_USER"]
    message["Subject"] = subject
    message.set_content(body)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    request = urllib.request.Request(
        _SEND_URL, data=json.dumps({"raw": raw}).encode(), method="POST"
    )
    request.add_header("Authorization", f"Bearer {_access_token()}")
    request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Gmail rejected the message: {exc}") from exc

    return {"provider": "gmail", "message_id": data.get("id", ""), "to": to}

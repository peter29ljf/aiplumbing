"""Telegram adapter — how we reach the technician.

Everything the office needs to say to a technician goes here rather than by SMS, because
it is free, it threads, and it can carry an address and a photo without three texts.

Reached through the gate like every other real service.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from plumbing.integrations.gate import LiveToolUnavailable, require_env

_API = "https://api.telegram.org/bot{token}/{method}"


def _call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = require_env("TELEGRAM_BOT_TOKEN")["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(_API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise LiveToolUnavailable(f"Telegram rejected {method}: {exc.read()[:300]!r}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Telegram unreachable: {exc}") from exc
    if not body.get("ok"):
        raise LiveToolUnavailable(f"Telegram returned an error: {body.get('description')}")
    return body.get("result") or {}


def send_message(chat_id: str, text: str) -> dict[str, Any]:
    """Send a message to a technician's chat. Returns the provider's message id."""
    if not chat_id:
        raise LiveToolUnavailable(
            "No Telegram chat id for this technician. They need to message the bot once "
            "so it learns their id, and that id has to be on their record."
        )
    result = _call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    return {"provider": "telegram", "message_id": str(result.get("message_id", ""))}


def verify_webhook_secret(provided: str) -> None:
    """Telegram echoes a shared secret on every update. Check it, in constant time.

    The webhook is a public URL as far as the internet is concerned. Without this anyone
    could post a forged update and drive the agent as an allowlisted technician. Refusing
    to serve when the secret is unset is deliberate — an endpoint with no shared secret
    accepts updates from anyone, which is worse than being switched off.
    """
    import hmac  # noqa: PLC0415

    expected = require_env("TELEGRAM_WEBHOOK_SECRET")["TELEGRAM_WEBHOOK_SECRET"]
    if not hmac.compare_digest(provided or "", expected):
        raise LiveToolUnavailable("Telegram webhook secret did not match.")

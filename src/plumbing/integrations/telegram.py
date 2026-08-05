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


def send_message(
    chat_id: str, text: str, buttons: list[list[dict[str, str]]] | None = None
) -> dict[str, Any]:
    """Send a message, optionally with inline buttons under it.

    `buttons` is rows of `{"text": ..., "data": ...}`. Telegram caps `callback_data` at
    **64 bytes**, and silently rejects the whole message when it is over — so the data
    carried is a short code, not a payload.
    """
    if not chat_id:
        raise LiveToolUnavailable(
            "No Telegram chat id for this technician. They need to message the bot once "
            "so it learns their id, and that id has to be on their record."
        )
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        for row in buttons:
            for button in row:
                if len(button["data"].encode()) > 64:
                    raise LiveToolUnavailable(
                        f"callback_data {button['data']!r} is over Telegram's 64-byte limit."
                    )
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [
                [{"text": b["text"], "callback_data": b["data"]} for b in row] for row in buttons
            ]
        })
    result = _call("sendMessage", payload)
    return {"provider": "telegram", "message_id": str(result.get("message_id", ""))}


def answer_callback(callback_id: str, text: str = "") -> None:
    """Acknowledge a button press.

    Not optional: until this is sent the client shows a spinner on the button and the
    technician cannot tell whether their tap registered, so they tap it again.
    """
    _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def edit_message(chat_id: str, message_id: str, text: str) -> None:
    """Replace a message and drop its buttons.

    Called the moment a decision lands, so the same job cannot be accepted twice by
    somebody scrolling back to an old offer.
    """
    _call("editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
    })


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

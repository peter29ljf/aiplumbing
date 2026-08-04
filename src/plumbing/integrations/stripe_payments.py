"""Stripe adapter for the refundable deposit.

Every call here moves real money. The refund path deliberately has no "force" option —
the rule that a technician already en route means supervisor review is enforced in the
world layer, and this adapter is not a way around it.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from plumbing.integrations.gate import LiveToolUnavailable, require_env

_BASE = "https://api.stripe.com/v1"


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    env = require_env("STRIPE_SECRET_KEY")
    data = urllib.parse.urlencode(payload, doseq=True).encode()
    request = urllib.request.Request(f"{_BASE}/{path}", data=data, method="POST")
    request.add_header("Authorization", f"Bearer {env['STRIPE_SECRET_KEY']}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Stripe call to {path} failed: {exc}") from exc


def _get(path: str) -> dict[str, Any]:
    env = require_env("STRIPE_SECRET_KEY")
    request = urllib.request.Request(f"{_BASE}/{path}", method="GET")
    request.add_header("Authorization", f"Bearer {env['STRIPE_SECRET_KEY']}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Stripe call to {path} failed: {exc}") from exc


def create_deposit_link(amount: float, currency: str, ticket_id: str) -> dict[str, Any]:
    """A Checkout session for the refundable deposit."""
    import os  # noqa: PLC0415

    base_url = os.environ.get("PUBLIC_BASE_URL", "https://example.com").rstrip("/")
    session = _post(
        "checkout/sessions",
        {
            "mode": "payment",
            "success_url": f"{base_url}/paid?ticket={ticket_id}",
            "cancel_url": f"{base_url}/cancelled?ticket={ticket_id}",
            "client_reference_id": ticket_id,
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": int(round(amount * 100)),
            "line_items[0][price_data][product_data][name]": "Refundable service deposit",
            "line_items[0][quantity]": 1,
            "metadata[ticket_id]": ticket_id,
        },
    )
    return {
        "provider": "stripe",
        "payment_id": session.get("id", ""),
        "link": session.get("url", ""),
        "amount": amount,
        "currency": currency,
    }


def check_payment(session_id: str) -> dict[str, Any]:
    session = _get(f"checkout/sessions/{session_id}")
    paid = session.get("payment_status") == "paid"
    return {
        "provider": "stripe",
        "payment_id": session_id,
        "paid": paid,
        "status": "paid" if paid else session.get("payment_status", "pending"),
        "payment_intent": session.get("payment_intent"),
    }


def refund_payment(payment_intent: str, reason: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"payment_intent": payment_intent}
    if reason:
        payload["metadata[reason]"] = reason
    refund = _post("refunds", payload)
    return {
        "provider": "stripe",
        "refund_id": refund.get("id", ""),
        "status": refund.get("status", ""),
        "amount": (refund.get("amount") or 0) / 100,
    }

"""Gmail adapter, using an OAuth refresh token.

Mirrors the aiphone project's approach: a refresh token is exchanged for a short-lived
access token on each call, so nothing long-lived sits in memory.

Sending was here first. Reading was added because half of `email.request_materials` is
useless without it: we can ask a supplier for a price, and then have no way of knowing they
answered. Both directions are capped — see MAX_RESULTS and MAX_BODY_CHARS.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any

from bat.live.integrations.gate import LiveToolUnavailable, require_env

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_SEND_URL = f"{_API}/messages/send"

# A reply is only worth reading in full for so long, and a mailbox is somebody's whole
# working life. Both ceilings are here rather than left to the caller: an agent asking for
# "everything from this supplier" would otherwise pull years of mail into a prompt.
MAX_RESULTS = 10
MAX_BODY_CHARS = 4000


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


def _get(path: str, params: dict[str, Any], token: str) -> dict[str, Any]:
    url = f"{_API}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Gmail would not answer: {exc}") from exc


def _header(payload: dict[str, Any], name: str) -> str:
    for header in payload.get("headers") or []:
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def _plain_text(payload: dict[str, Any]) -> str:
    """The readable part of a MIME tree.

    Gmail returns multipart mail as nested parts, and the same message usually appears
    twice: once as text and once as HTML. Preferring the text part is not cosmetic — the
    HTML alternative of a short reply is routinely tens of kilobytes of styling, and a
    caller that takes whichever part comes first pays for that in the prompt.
    """
    plain = _first_part(payload, "text/plain")
    if plain:
        return plain

    html = _first_part(payload, "text/html")
    if not html:
        return ""
    # Stripping tags leaves the layout whitespace behind — runs of newlines and indentation
    # that meant something to a renderer and nothing to a reader. Collapsing costs no
    # information here: the structure went with the tags.
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _first_part(payload: dict[str, Any], mime: str) -> str:
    """Depth-first search of the whole tree for one MIME type.

    Two passes rather than one recursion that returns whatever it finds first: in a
    multipart/alternative the HTML version is usually listed *after* the text one, but not
    always, and a single pass that accepts either returns whichever the sender happened to
    put first. The whole point is to prefer text wherever it is.
    """
    body = payload.get("body") or {}
    if payload.get("mimeType") == mime and body.get("data"):
        return base64.urlsafe_b64decode(body["data"]).decode("utf-8", "replace")

    for part in payload.get("parts") or []:
        found = _first_part(part, mime)
        if found:
            return found
    return ""


def search_email(query: str, *, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """Gmail search syntax, e.g. `from:supplier@x.com newer_than:7d`.

    Returns headers only. Reading every hit in full would mean one API call per result and
    a prompt full of mail nobody asked for; the caller picks what to open.
    """
    token = _access_token()
    listing = _get(
        "messages",
        {"q": query, "maxResults": max(1, min(int(limit), MAX_RESULTS))},
        token,
    )

    found = []
    for stub in listing.get("messages") or []:
        detail = _get(
            f"messages/{stub['id']}",
            {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
            token,
        )
        payload = detail.get("payload") or {}
        found.append({
            "message_id": stub["id"],
            "thread_id": detail.get("threadId", ""),
            "from": _header(payload, "From"),
            "subject": _header(payload, "Subject"),
            "date": _header(payload, "Date"),
            "snippet": detail.get("snippet", ""),
        })
    return found


def read_email(message_id: str) -> dict[str, Any]:
    """One message, in full — truncated, and saying so when it is.

    Silent truncation is the failure that matters here: an agent that reads the first four
    thousand characters of a quote and answers as though it read all of it will quote the
    wrong figure with complete confidence.
    """
    detail = _get(f"messages/{message_id}", {"format": "full"}, _access_token())
    payload = detail.get("payload") or {}
    body = _plain_text(payload).strip()
    truncated = len(body) > MAX_BODY_CHARS

    return {
        "message_id": message_id,
        "thread_id": detail.get("threadId", ""),
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "body": body[:MAX_BODY_CHARS] + ("\n[... truncated ...]" if truncated else ""),
        "truncated": truncated,
    }

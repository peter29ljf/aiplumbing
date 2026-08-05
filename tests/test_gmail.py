"""Reading mail: the MIME tree, and the two ceilings.

No network. These are about what the adapter does with what Gmail returns — which is where
the mistakes are, since the API itself either answers or raises.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.integrations import gmail_email  # noqa: E402


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _alternative(*, html_first: bool) -> dict:
    text = {"mimeType": "text/plain", "body": {"data": _b64("the real text")}}
    html = {"mimeType": "text/html", "body": {"data": _b64("<p>styled</p>")}}
    inner = [html, text] if html_first else [text, html]
    return {"mimeType": "multipart/mixed",
            "parts": [{"mimeType": "multipart/alternative", "parts": inner}]}


def test_the_text_part_wins_whichever_order_it_arrives_in():
    """The HTML alternative of a short reply is routinely tens of kilobytes of styling.
    A single pass that takes whichever part comes first pays for that in the prompt."""
    assert gmail_email._plain_text(_alternative(html_first=False)) == "the real text"
    assert gmail_email._plain_text(_alternative(html_first=True)) == "the real text"


def test_html_is_read_when_there_is_nothing_else():
    """Tags out, and the layout whitespace they left behind collapsed with them."""
    body = "<div>\n  <p>hello <b>there</b></p>\n</div>"
    payload = {"mimeType": "text/html", "body": {"data": _b64(body)}}
    assert gmail_email._plain_text(payload) == "hello there"


def test_a_message_with_no_readable_part_is_empty_rather_than_a_crash():
    assert gmail_email._plain_text({"mimeType": "image/png", "body": {"attachmentId": "x"}}) == ""


def test_headers_are_found_however_they_are_cased():
    payload = {"headers": [{"name": "SUBJECT", "value": "Quote for 40m of copper"}]}
    assert gmail_email._header(payload, "subject") == "Quote for 40m of copper"


def test_a_missing_header_is_blank_not_an_error():
    assert gmail_email._header({}, "from") == ""


def test_a_long_message_says_that_it_was_cut(monkeypatch):
    """The failure that matters: an agent that reads the first four thousand characters of
    a quote and answers as though it read all of it quotes the wrong figure confidently."""
    long_body = "x" * (gmail_email.MAX_BODY_CHARS + 500)
    monkeypatch.setattr(gmail_email, "_access_token", lambda: "token")
    monkeypatch.setattr(gmail_email, "_get", lambda *a, **k: {
        "threadId": "t1",
        "payload": {"mimeType": "text/plain", "body": {"data": _b64(long_body)}},
    })

    message = gmail_email.read_email("m1")

    assert message["truncated"] is True
    assert message["body"].endswith("[... truncated ...]")


def test_a_short_message_is_not_marked_truncated(monkeypatch):
    monkeypatch.setattr(gmail_email, "_access_token", lambda: "token")
    monkeypatch.setattr(gmail_email, "_get", lambda *a, **k: {
        "payload": {"mimeType": "text/plain", "body": {"data": _b64("short")}},
    })

    message = gmail_email.read_email("m1")

    assert message["truncated"] is False
    assert message["body"] == "short"


def test_a_search_cannot_be_talked_into_reading_the_whole_mailbox(monkeypatch):
    """`limit` comes from a caller that may be an agent acting on what somebody typed."""
    asked: list[dict] = []

    def fake_get(path, params, token):
        asked.append(params)
        return {"messages": []} if path == "messages" else {"payload": {}}

    monkeypatch.setattr(gmail_email, "_access_token", lambda: "token")
    monkeypatch.setattr(gmail_email, "_get", fake_get)

    gmail_email.search_email("from:supplier@example.com", limit=5000)

    assert asked[0]["maxResults"] == gmail_email.MAX_RESULTS


def test_a_search_asks_for_headers_only(monkeypatch):
    """One full fetch per hit is a prompt full of mail nobody asked for."""
    formats: list[str] = []

    def fake_get(path, params, token):
        if path == "messages":
            return {"messages": [{"id": "m1"}]}
        formats.append(params["format"])
        return {"threadId": "t1", "snippet": "...", "payload": {"headers": []}}

    monkeypatch.setattr(gmail_email, "_access_token", lambda: "token")
    monkeypatch.setattr(gmail_email, "_get", fake_get)

    found = gmail_email.search_email("newer_than:7d")

    assert formats == ["metadata"]
    assert found[0]["message_id"] == "m1"

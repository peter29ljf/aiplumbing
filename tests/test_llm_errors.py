"""Telling "the provider said no" apart from "we never reached the provider".

The remedy is somewhere else entirely, and the message that used to come back sent people
to check whether the model still existed while their own resolver was down.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.llm import _is_connection_problem, _is_fatal  # noqa: E402


class WithStatus(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    "message",
    [
        "[Errno 8] nodename nor servname provided, or not known",   # macOS
        "[Errno -3] Temporary failure in name resolution",          # Linux
        "Connection error.",                                        # the SDK's own wording
        "getaddrinfo failed",
        "Request timed out.",
        "SSL: CERTIFICATE_VERIFY_FAILED",
    ],
)
def test_the_request_never_arrived(message: str):
    assert _is_connection_problem(Exception(message))


def test_an_answer_from_the_provider_is_not_a_connection_problem():
    """It has a status code, so it got there — whatever it says."""
    assert not _is_connection_problem(WithStatus("no such model", 404))
    assert not _is_connection_problem(WithStatus("rate limited", 429))


def test_a_message_about_something_else_is_not_swept_in():
    assert not _is_connection_problem(Exception("the model refused to return JSON"))


def test_what_is_fatal_did_not_change():
    """Auth and missing models never succeed on retry; a rate limit and a dead socket do."""
    assert _is_fatal(WithStatus("bad key", 401))
    assert _is_fatal(WithStatus("no such model", 404))
    assert not _is_fatal(WithStatus("rate limited", 429))
    assert not _is_fatal(Exception("Connection error."))

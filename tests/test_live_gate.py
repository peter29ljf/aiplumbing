"""Which tools reach the outside world, and where that decision comes from."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.integrations import gate  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(gate.ENV_MASTER, raising=False)
    monkeypatch.delenv(gate.ENV_TOOLS, raising=False)
    # load_dotenv would put the developer's own .env back under the test.
    monkeypatch.setattr(gate, "load_dotenv", lambda: None)


def _catalog(monkeypatch, *, enabled: bool, live: list[str]):
    monkeypatch.setattr(gate, "_catalog", lambda: {
        "live_tools_enabled": enabled,
        "statuses": {name: "live" for name in live},
    })


# ---- the default -----------------------------------------------------


def test_everything_is_mocked_when_nothing_says_otherwise(monkeypatch):
    _catalog(monkeypatch, enabled=False, live=[])
    assert gate.master_enabled() is False
    assert gate.is_live("telegram.send") is False


def test_the_file_still_works_when_the_environment_is_silent(monkeypatch):
    _catalog(monkeypatch, enabled=True, live=["telegram.send"])
    assert gate.is_live("telegram.send") is True
    assert gate.is_live("sms.send") is False


# ---- the environment wins --------------------------------------------


def test_the_environment_overrides_the_file(monkeypatch):
    """The whole point: pulling code must not change what reaches the outside world."""
    _catalog(monkeypatch, enabled=False, live=[])
    monkeypatch.setenv(gate.ENV_MASTER, "true")
    monkeypatch.setenv(gate.ENV_TOOLS, "telegram.send,sms.send")

    assert gate.is_live("telegram.send") is True
    assert gate.is_live("sms.send") is True
    assert gate.is_live("payment.send_deposit_link") is False


def test_the_environment_can_switch_everything_off(monkeypatch):
    """A machine must be able to go quiet without editing a tracked file."""
    _catalog(monkeypatch, enabled=True, live=["telegram.send"])
    monkeypatch.setenv(gate.ENV_MASTER, "false")
    assert gate.is_live("telegram.send") is False


def test_an_empty_tool_list_means_none_not_all(monkeypatch):
    _catalog(monkeypatch, enabled=True, live=["telegram.send"])
    monkeypatch.setenv(gate.ENV_TOOLS, "")
    assert gate.is_live("telegram.send") is False


def test_a_typo_in_the_master_switch_reads_as_off(monkeypatch):
    """Fail closed. A misspelling must never turn real services on."""
    _catalog(monkeypatch, enabled=False, live=[])
    for typo in ("ture", "enabled", "y", "TRUE!"):
        monkeypatch.setenv(gate.ENV_MASTER, typo)
        assert gate.master_enabled() is False, typo


def test_common_spellings_of_on_are_accepted(monkeypatch):
    _catalog(monkeypatch, enabled=False, live=[])
    for spelling in ("true", "TRUE", " True ", "1", "yes", "on"):
        monkeypatch.setenv(gate.ENV_MASTER, spelling)
        assert gate.master_enabled() is True, spelling


def test_the_master_switch_still_gates_everything(monkeypatch):
    """A tool named in the environment is still off if the master switch is."""
    _catalog(monkeypatch, enabled=False, live=[])
    monkeypatch.setenv(gate.ENV_TOOLS, "telegram.send")
    assert gate.is_live("telegram.send") is False


# ---- the console must not lie ----------------------------------------


def test_the_status_says_where_each_answer_came_from(monkeypatch):
    """A console claiming a tool is live while the process disagrees is worse than none:
    somebody reads it, believes the technician is being notified, and stops checking."""
    _catalog(monkeypatch, enabled=False, live=[])
    monkeypatch.setenv(gate.ENV_MASTER, "true")
    monkeypatch.setenv(gate.ENV_TOOLS, "telegram.send")

    status = gate.live_status()
    assert status["master_switch"] is True
    assert status["master_switch_source"] == "env"
    assert status["tools_source"] == "env"
    assert status["effectively_live"] == ["telegram.send"]


def test_the_status_reports_the_file_when_that_is_what_is_used(monkeypatch):
    _catalog(monkeypatch, enabled=True, live=["telegram.send"])
    status = gate.live_status()
    assert status["master_switch_source"] == "file"
    assert status["tools_source"] == "file"


def test_nothing_is_effectively_live_while_the_master_switch_is_off(monkeypatch):
    _catalog(monkeypatch, enabled=False, live=["telegram.send", "sms.send"])
    status = gate.live_status()
    assert status["effectively_live"] == []
    assert status["tools_marked_live"] == ["sms.send", "telegram.send"]

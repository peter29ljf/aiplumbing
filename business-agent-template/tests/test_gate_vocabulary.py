"""The switch is a string lookup, and the failure mode is silence.

`is_live("calendar.create")` against a gate that knows `calendar.create_appointment` does
not raise, does not log, and does not warn. It returns False, every booking stays
simulated, and the only way anybody finds out is a customer ringing to ask where the
plumber is. An earlier tree in this repository had exactly that mismatch.

A typo in a systemd unit on a machine nobody is watching does the same thing.

So there is one vocabulary, `gate.KNOWN_TOOLS`, and three things check against it:

- the world may only ask about names in it
- the environment may only name names in it, and says so loudly otherwise
- nothing is live unless the master switch is on as well

Costs nothing to run: no model, no network, no credentials.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.live.integrations import gate  # noqa: E402
from bat.live.world import SWITCHES  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """`.env` at the project root is real and holds real credentials. Every test here sets
    both variables explicitly so none of them can accidentally read it."""
    monkeypatch.setenv(gate.ENV_MASTER, "false")
    monkeypatch.setenv(gate.ENV_TOOLS, "")


# ---- the two lists have to agree ----------------------------------------


def test_every_switch_the_world_reaches_for_is_one_the_gate_knows():
    """The bug this file exists for. A name the gate does not know is mocked forever."""
    unknown = sorted(set(SWITCHES.values()) - gate.KNOWN_TOOLS)

    assert not unknown, (
        f"bat/live/world.py asks about {unknown}, which gate.KNOWN_TOOLS does not list. "
        f"Those acts would stay simulated with nothing anywhere saying so."
    )


def test_the_outward_acts_all_have_a_switch():
    """A method that reaches a real service and is not in SWITCHES cannot be turned off,
    which is worse than one that cannot be turned on."""
    assert set(SWITCHES) == {"free_slots", "book", "send_sms", "send_email",
                             "notify_technician", "escalate"}


# ---- what the environment may say ---------------------------------------


def test_a_name_nobody_recognises_is_refused_out_loud(monkeypatch):
    monkeypatch.setenv(gate.ENV_MASTER, "true")
    monkeypatch.setenv(gate.ENV_TOOLS, "calendar.create")

    with pytest.raises(gate.UnknownLiveTool) as refused:
        gate.is_live("calendar.create_appointment")

    assert "calendar.create_appointment" in str(refused.value), "no correction offered"


def test_a_real_name_alongside_a_typo_does_not_rescue_it(monkeypatch):
    """Otherwise half a deployment is live and half is silently not."""
    monkeypatch.setenv(gate.ENV_MASTER, "true")
    monkeypatch.setenv(gate.ENV_TOOLS, "sms.send,telegram.snd")

    with pytest.raises(gate.UnknownLiveTool):
        gate.is_live("sms.send")


# ---- fail closed --------------------------------------------------------


def test_nothing_is_live_by_default():
    assert gate.master_enabled() is False
    assert not any(gate.is_live(name) for name in gate.KNOWN_TOOLS)


def test_the_master_switch_alone_turns_nothing_on(monkeypatch):
    monkeypatch.setenv(gate.ENV_MASTER, "true")

    assert not any(gate.is_live(name) for name in gate.KNOWN_TOOLS)


def test_naming_a_tool_alone_turns_it_on_only_with_the_master_switch(monkeypatch):
    monkeypatch.setenv(gate.ENV_TOOLS, "sms.send")
    assert not gate.is_live("sms.send")

    monkeypatch.setenv(gate.ENV_MASTER, "true")
    assert gate.is_live("sms.send")


def test_a_typo_in_the_master_switch_reads_as_off(monkeypatch):
    """`PLUMBING_LIVE_ENABLED=ture` must not send anybody a text."""
    monkeypatch.setenv(gate.ENV_MASTER, "ture")
    monkeypatch.setenv(gate.ENV_TOOLS, "sms.send")

    assert gate.is_live("sms.send") is False


def test_an_empty_tool_list_turns_everything_off(monkeypatch):
    """The one-word way to stop a live system in a hurry."""
    monkeypatch.setenv(gate.ENV_MASTER, "true")
    monkeypatch.setenv(gate.ENV_TOOLS, "")

    assert not any(gate.is_live(name) for name in gate.KNOWN_TOOLS)


# ---- what a person reads before each stage of a rollout -----------------


def test_the_status_says_what_is_live_and_where_it_was_read_from(monkeypatch):
    monkeypatch.setenv(gate.ENV_MASTER, "true")
    monkeypatch.setenv(gate.ENV_TOOLS, "calendar.find_slots,sms.send")

    status = gate.live_status()

    assert status["master_switch"] is True
    assert status["effectively_live"] == ["calendar.find_slots", "sms.send"]
    assert gate.ENV_TOOLS in status["source"]


def test_the_status_does_not_call_something_live_while_the_master_switch_is_off(monkeypatch):
    """The screen that lies is the thing this function was written to prevent — somebody
    reads "mocked", believes nothing is going out, and stops checking."""
    monkeypatch.setenv(gate.ENV_MASTER, "false")
    monkeypatch.setenv(gate.ENV_TOOLS, "sms.send")

    status = gate.live_status()

    assert status["effectively_live"] == []
    assert status["tools_marked_live"] == ["sms.send"], "the intent is still worth showing"

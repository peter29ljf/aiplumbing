"""The console's switches, and what a tool's status light actually promises.

Two things are being pinned here, and both have already been wrong once.

**A tool answers to a service, not to its own name.** `technician.notify` and
`escalate.raise` both reach a person through Telegram, so both stop the moment Telegram
does. Reading the tool's own name instead had the console reporting `technician.notify` as
simulated while it was messaging a real technician — the kind of thing somebody reads once
and then stops checking.

**Switched on and unreachable is its own state.** A tool whose switch is on and whose
credentials are missing is not live and is not simulated: the agent will call it, be
refused, and stop in the middle of somebody's booking. That is worth a colour of its own,
so it is worth a test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from flow.live.world import GATES  # noqa: E402
from plumbing.dashboard import server as console  # noqa: E402
from plumbing.integrations import gate  # noqa: E402


@pytest.fixture(autouse=True)
def switches_off(monkeypatch):
    """Every test starts simulated and cannot leak its state into the next one.

    `set_switches` writes the real environment on purpose — that is how the console
    changes what this process does — so without this a test that armed the gate would arm
    it for the rest of the session, and `test_live_world.py` would start sending texts.
    """
    monkeypatch.setenv(gate.ENV_MASTER, "false")
    monkeypatch.setenv(gate.ENV_TOOLS, "")


@pytest.fixture(autouse=True)
def dotenv_stays_out_of_it(monkeypatch):
    """These tests own the environment.

    `preflight` reads `.env` every time on purpose — somebody who pastes a credential in
    and reloads the console should see the light change without restarting anything. That
    is right and it makes the developer's own `.env` an invisible input to every assertion
    below, so it is held still here rather than worked around in each test.
    """
    monkeypatch.setattr(gate, "load_dotenv", lambda: None)


@pytest.fixture()
def credentials(monkeypatch):
    """Everything present, so `blocked` has to come from the switch and not the .env."""
    for names, _ in gate.NEEDS.values():
        for name in names:
            monkeypatch.setenv(name, "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")


def _by_name(overview):
    return {t["name"]: t for t in overview["tools"]}


# ---- which switch a tool answers to -----------------------------------


def test_reaching_a_technician_answers_to_telegram_not_to_its_own_name(credentials):
    console.set_live({"tools": ["telegram.send"]})
    tools = _by_name(console.tools_overview())

    assert tools["technician.notify"]["status"] == "live"
    assert tools["escalate.raise"]["status"] == "live"
    assert tools["technician.notify"]["gate"] == "telegram.send"


def test_turning_a_service_off_takes_every_tool_behind_it(credentials):
    console.set_live({"tools": ["telegram.send"]})
    console.set_live({"tools": []})
    tools = _by_name(console.tools_overview())

    assert tools["technician.notify"]["status"] == "simulated"
    assert tools["escalate.raise"]["status"] == "simulated"


def test_every_gate_key_is_one_the_switches_offer():
    """A tool pointing at a switch nobody can flip is a tool stuck simulated forever."""
    offered = {s["gate"] for s in console.tools_overview()["services"]}
    assert set(GATES.values()) == offered


def test_a_tool_that_never_leaves_the_process_is_local(credentials):
    console.set_live({"all": True})
    tools = _by_name(console.tools_overview())

    # The database is this deployment's own record, not a switch anybody flips.
    for name in ("ticket.set_fields", "crm.lookup_by_phone", "clock.now",
                 "rules.get_job_sizing", "step.finished", "calendar.find_booking"):
        assert tools[name]["status"] == "local", name
        assert tools[name]["gate"] == ""


# ---- the light ---------------------------------------------------------


def test_switched_on_with_no_credentials_reads_blocked_not_live(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    console.set_live({"tools": ["telegram.send"]})
    tools = _by_name(console.tools_overview())

    assert tools["technician.notify"]["status"] == "blocked"
    assert "TELEGRAM_BOT_TOKEN" in tools["technician.notify"]["blocker"]


def test_the_preflight_sends_nothing(monkeypatch, credentials):
    """It is called on every page load. A check that reached Twilio would bill for it."""
    import urllib.request

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("preflight must not open a connection")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    for service in set(GATES.values()):
        gate.preflight(service)


def test_missing_credentials_are_named_one_by_one(monkeypatch, credentials):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)

    blocker = gate.preflight("sms.send")
    assert "TWILIO_AUTH_TOKEN" in blocker and "TWILIO_PHONE_NUMBER" in blocker
    assert "TWILIO_ACCOUNT_SID" not in blocker         # that one is present


# ---- the switch itself -------------------------------------------------


def test_the_master_switch_arms_every_service(credentials):
    console.set_live({"all": True})
    assert all(s["on"] for s in console.tools_overview()["services"])


def test_an_empty_list_disarms_the_master_switch(credentials):
    """A master switch on with nothing behind it reads as armed and is not."""
    console.set_live({"all": True})
    console.set_live({"tools": []})

    assert gate.master_enabled() is False
    assert console.live_switches()["effectively_live"] == []


def test_a_switch_nobody_defined_is_refused_rather_than_ignored():
    """Silently dropping it would leave the console showing a service it did not turn on."""
    with pytest.raises(ValueError, match="Not a switch"):
        console.set_live({"tools": ["stripe.charge"]})


def test_the_switches_reach_the_gate_the_tools_actually_read(credentials):
    """The point of the whole feature: flipping the console changes what LiveWorld does.

    A console that set its own flag and left `is_live` answering no would show every light
    green while every send went nowhere.
    """
    assert gate.is_live("sms.send") is False
    console.set_live({"all": True})
    assert gate.is_live("sms.send") is True


def test_nothing_is_written_to_disk(credentials):
    """Production's switches live in the systemd unit. A console that wrote a tracked file
    would put them back in git, which is what once let a `git pull` silently stop Telegram
    notifications with nothing anywhere reporting an error."""
    catalog = ROOT / "config" / "tool_catalog.yaml"
    before = catalog.read_text(encoding="utf-8")

    console.set_live({"all": True})

    assert catalog.read_text(encoding="utf-8") == before
    assert console.live_switches()["tools_source"] == "env"

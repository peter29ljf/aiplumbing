"""Only some agents run in production, and everything that could route to one must know."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing import agent_registry, config  # noqa: E402
from plumbing.tools.ops_tools import handoff_transfer  # noqa: E402
from plumbing.tools.registry import ToolContext  # noqa: E402
from plumbing.world import ToolRejection, World  # noqa: E402

LIVE = ("intake", "small_job")


def _ctx(enabled=LIVE):
    world = World("2026-08-05T10:00:00-07:00")
    world.create_ticket("+16047218629")
    ctx = ToolContext(world=world, agent_name="intake", enabled_agents=enabled)
    ctx.scenario = {}
    return ctx


# ---- the tool ---------------------------------------------------------


def test_a_transfer_to_an_agent_nobody_is_running_is_refused():
    """It used to succeed, then the conversation layer said it had not.

    The tool returned {"transferred_to": "warranty", "message": "Your part is done"},
    wrote a handoff into world state, and only then did the layer above inject "'warranty'
    is not available". Being told a thing worked and then that it did not is the hardest
    kind of instruction to recover from.
    """
    ctx = _ctx()
    with pytest.raises(ToolRejection) as caught:
        handoff_transfer(ctx, to_agent="warranty", reason="claim", summary="s")

    message = str(caught.value)
    assert "not running in this deployment" in message
    assert "escalate.raise" in message           # and it says what to do instead
    assert sorted(LIVE)[0] in message            # and which agents can be reached


def test_a_refused_transfer_leaves_no_phantom_handoff():
    """A handoff in world state that never happened is a lie the audit log tells later."""
    ctx = _ctx()
    with pytest.raises(ToolRejection):
        handoff_transfer(ctx, to_agent="emergency", reason="urgent", summary="s")
    assert ctx.world.handoffs == []


def test_a_transfer_to_a_running_agent_still_works():
    ctx = _ctx()
    result = handoff_transfer(ctx, to_agent="small_job", reason="booked", summary="s")
    assert result["transferred_to"] == "small_job"
    assert len(ctx.world.handoffs) == 1


def test_the_test_rig_is_not_restricted():
    """A suite that only covered what production switched on would stop covering the rest."""
    ctx = _ctx(enabled=None)
    assert handoff_transfer(ctx, to_agent="warranty", reason="claim", summary="s")


# ---- the system prompt ------------------------------------------------


def test_the_prompt_does_not_advertise_an_agent_that_is_off():
    """The model has no other way to know, so offering it invites a dead transfer."""
    live = agent_registry.build_system_prompt("intake", enabled=set(LIVE))
    offered = live.split("You may hand off to:")[-1][:400]
    for switched_off in ("warranty", "large_job", "emergency"):
        assert f"`{switched_off}`" not in offered
    assert "`small_job`" in offered


def test_the_prompt_and_the_tool_agree():
    """Two lists that can drift are two lists that will."""
    from plumbing.llm import LLM

    agents = agent_registry.build_all(LLM.__new__(LLM), enabled=set(LIVE))
    assert set(agents["intake"].spec.handoff_to) <= set(LIVE)


# ---- the config -------------------------------------------------------


def test_the_enabled_set_comes_from_config_not_a_hardcoded_tuple():
    assert config.enabled_agents() == list(LIVE)


def test_no_live_config_means_everything_runs(monkeypatch):
    """A checkout without live.yaml is a full system, not an empty one."""
    monkeypatch.setattr(config, "live_config", lambda: {})
    assert set(config.enabled_agents()) == set(config.agents_config()["agents"])

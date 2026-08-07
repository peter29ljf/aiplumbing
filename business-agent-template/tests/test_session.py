"""The phase machine and the stop conditions. No model, no subprocess.

These are the rules that decide when a build is finished and when it should stop and ask,
and both of them are decisions somebody has to be able to argue with. Testing them costs
milliseconds; discovering them wrong costs an unattended night.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.builder import session  # noqa: E402

RULES = {
    "usable": {
        "every_scenario_clean": True,
        "min_pass_rate": 0.95,
        "config_faults": 0,
        "stop_after_flat_rounds": 2,
    }
}


# ---- what counts as finished -------------------------------------------


def test_every_scenario_clean_is_enough():
    assert session.usable(0.9, config_faults=0, every_clean=True, rules=RULES)


def test_a_high_enough_pass_rate_is_enough_on_its_own():
    assert session.usable(0.96, config_faults=0, every_clean=False, rules=RULES)


def test_not_quite_the_pass_rate_is_not_finished():
    assert not session.usable(0.94, config_faults=0, every_clean=False, rules=RULES)


def test_one_configuration_fault_blocks_a_perfect_score():
    """The rule that does not bend. A config fault is a rules file and a tool list
    contradicting each other — the generator's own bug — and every scenario passing
    anyway just means no scenario walks into it yet."""
    assert not session.usable(1.0, config_faults=1, every_clean=True, rules=RULES)


def test_a_project_can_loosen_the_pass_rate_but_the_faults_stay():
    lenient = {"usable": {**RULES["usable"], "min_pass_rate": 0.5}}

    assert session.usable(0.6, config_faults=0, every_clean=False, rules=lenient)
    assert not session.usable(0.6, config_faults=1, every_clean=False, rules=lenient)


# ---- when to give up ----------------------------------------------------


def test_improvement_is_measured_against_the_best_not_the_last(tmp_path, monkeypatch):
    """86 then 84 then 85 is two rounds without improvement, however the third looks
    against the second. Measured against the last, that middle dip would reset the count
    and the loop would run forever on noise."""
    monkeypatch.setattr(session, "PROJECTS", tmp_path)
    build = session.Build(name="x")

    for score in (0.86, 0.84, 0.85):
        session.note_round(build, score, RULES)

    assert build.best_score == 0.86
    assert build.flat_rounds == 2
    assert session.give_up(build, RULES)


def test_a_real_gain_resets_the_count(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "PROJECTS", tmp_path)
    build = session.Build(name="x")

    for score in (0.86, 0.84, 0.90):
        session.note_round(build, score, RULES)

    assert build.flat_rounds == 0
    assert not session.give_up(build, RULES)


# ---- the phases ---------------------------------------------------------


def test_the_plan_is_the_one_hard_stop(tmp_path, monkeypatch):
    """Everything after the plan writes files and spends money. This is the last point
    where redirecting is cheap, so it stops whether or not anyone asked it to."""
    monkeypatch.setattr(session, "PROJECTS", tmp_path)
    build = session.Build(name="x", phase=session.PLAN)

    session.advance(build)

    assert build.phase == session.BUILD
    assert build.waiting == session.WAITING_FOR_APPROVAL
    assert not build.running


def test_testing_and_fixing_go_round(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "PROJECTS", tmp_path)
    build = session.Build(name="x", phase=session.TEST)

    session.advance(build)
    assert build.phase == session.ITERATE

    session.advance(build)
    assert build.phase == session.TEST          # back for another run


def test_state_survives_the_console_being_restarted(tmp_path, monkeypatch):
    """An interview is a conversation somebody is half way through. Losing it because a
    process restarted means asking them the same questions again, which is the one thing
    the interview prompt tells the agent never to do."""
    monkeypatch.setattr(session, "PROJECTS", tmp_path)
    build = session.Build(name="dental", phase=session.INTERVIEW,
                          session_id="abc-123", note="what do you charge?")
    build.transcript.append({"said": "hi", "replied": "what do you charge?"})
    session.save(build)

    again = session.load("dental")

    assert again.session_id == "abc-123"
    assert again.phase == session.INTERVIEW
    assert again.transcript[0]["replied"] == "what do you charge?"


def test_an_unbuilt_project_starts_at_the_interview(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "PROJECTS", tmp_path)

    assert session.load("never-heard-of-it").phase == session.INTERVIEW


def test_starting_makes_a_shape_not_a_scaffold(tmp_path, monkeypatch):
    """Empty directories, no placeholder files. A half-written flow.yaml is something the
    builder has to read, understand and undo before it can write the real one."""
    monkeypatch.setattr(session, "PROJECTS", tmp_path)

    session.start("dental")

    assert (tmp_path / "dental" / "rules").is_dir()
    assert not (tmp_path / "dental" / "flow.yaml").exists()


# ---- the prompt ---------------------------------------------------------


@pytest.mark.parametrize("phase", [session.INTERVIEW, session.PLAN,
                                   session.BUILD, session.ITERATE])
def test_every_phase_carries_the_architecture(phase):
    """Same bytes in front of every call, which is what a prompt cache wants — and it is
    the half of the instructions that never changes between projects."""
    asked = session.prompt_for(phase, said="hello")

    assert "flow.yaml" in asked
    assert "Anything a node learns and does not write down is gone" in asked


def test_the_interview_is_told_not_to_design_yet():
    asked = session.prompt_for(session.INTERVIEW, said="here is my flowchart")

    assert "questions only" in asked


def test_a_report_is_handed_over_verbatim():
    """The fixing phase reads the harness output, not a summary of it. A summary is
    somebody's opinion about which failure mattered."""
    asked = session.prompt_for(session.ITERATE, said="", report="FAIL warranty_claim 2/4")

    assert "FAIL warranty_claim 2/4" in asked


def test_an_empty_account_says_so_rather_than_saying_broken(tmp_path, monkeypatch):
    """Nothing was lost when this happened for real: the phase and the session id are on
    disk, so topping up and running the same command again carries on. The state has to
    say that, because "failed" sends somebody looking for a bug that is not there."""
    monkeypatch.setattr(session, "PROJECTS", tmp_path)
    monkeypatch.setattr(session.claude, "run", lambda *a, **k: session.claude.Reply(
        error="Credit balance is too low"))
    build = session.Build(name="dental")

    session.turn(build, "build it")

    assert build.waiting == session.NO_CREDIT
    assert "top it up" in build.note
    assert session.load("dental").session_id == build.session_id   # kept, so --resume works


def test_the_verdict_reads_the_report_that_run_wrote(tmp_path, monkeypatch):
    """`runs/` is written by the builder's own spot checks as well as by the full suite,
    and one of those was newer. A project with fifteen scenarios was declared usable on a
    three-scenario report — the instrument lying about the product, which is the first
    thing METHOD warns about."""
    import json as _json

    from bat.builder import __main__ as cli

    mine = tmp_path / "20260101-000000.json"
    mine.write_text(_json.dumps([{"id": "a", "passed": True, "verdicts": []},
                                 {"id": "b", "passed": False, "verdicts": []}]))
    newer = tmp_path / "20260101-000001.json"      # a spot check, written later
    newer.write_text(_json.dumps([{"id": "a", "passed": True, "verdicts": []}]))

    class Done:
        stdout = f"1/2 passed\nfull record: {mine}\n"
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(cli.projects, "find",
                        lambda name: type("P", (), {"harness": lambda self: {},
                                                    "runs_dir": tmp_path})())

    _, rate, faults = cli._harness("x")

    assert rate == 0.5          # its own report, not the newer one that says 100%
    assert faults == 0


# ---- who answers for which role ------------------------------------------


def test_a_role_can_run_on_a_different_provider(monkeypatch):
    """The simulated customer runs on another family from the agent under test. A model
    judging conversations with a sibling of itself is a bias nobody can rule out from
    inside the run, and the measured cost of getting it wrong is large — unconstrained
    simulators match real users' style 6-8% of the time."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("QWEN_API_KEY", "y")

    from bat.runtime.llm import LLM

    llm = LLM({
        "active": "one",
        "providers": {
            "one": {"base_url": "https://one.example", "api_key_env": "DEEPSEEK_API_KEY",
                    "model": "model-one"},
            "two": {"base_url": "https://two.example", "api_key_env": "QWEN_API_KEY",
                    "model": "model-two"},
        },
        "roles": {"agent": {}, "customer": {"provider": "two"}},
    })

    agent_client, agent_model, _ = llm.for_role("agent")
    customer_client, customer_model, _ = llm.for_role("customer")

    assert agent_model == "model-one"
    # The role's provider must bring its own model. `role_settings` fills the active
    # provider's model in as a default, and taking that would call one endpoint with
    # another's model name.
    assert customer_model == "model-two"
    assert str(agent_client.base_url) != str(customer_client.base_url)


def test_a_role_naming_a_provider_nobody_configured_says_so(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")

    import pytest as _pytest

    from bat.runtime.llm import LLM, LLMError

    llm = LLM({
        "active": "one",
        "providers": {"one": {"base_url": "https://one.example",
                              "api_key_env": "DEEPSEEK_API_KEY", "model": "m"}},
        "roles": {"customer": {"provider": "nowhere"}},
    })

    with _pytest.raises(LLMError, match="nowhere"):
        llm.for_role("customer")

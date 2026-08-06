"""Reading what Claude Code says back, without asking it anything.

Every event here is the shape a real invocation produced. Nothing in this file starts a
process: the parsing is what breaks when the CLI changes, and it is worth being able to
check that in a second rather than in a paid minute.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.builder import claude  # noqa: E402

# Copied off `claude -p "Reply with exactly: ok" --output-format json --model haiku`.
# The field names are the reason this file exists — none of them is the obvious guess.
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "850734df-cf0a-4abb-8d3c-0d2e70d09885",
    "num_turns": 1,
    "duration_ms": 5909,
    "total_cost_usd": 0.01026745,
    "usage": {
        "input_tokens": 10,
        "output_tokens": 39,
        "cache_creation_input_tokens": 6639,
        "cache_read_input_tokens": 17637,
    },
    "permission_denials": [],
    "result": "ok",
}


def _fold(*events: dict) -> claude.Reply:
    reply = claude.Reply()
    for event in events:
        reply = claude.absorb(reply, event)
    return reply


def test_the_result_text_is_what_comes_back():
    assert _fold(RESULT).text == "ok"


def test_the_session_id_is_kept_so_the_next_turn_can_resume():
    assert _fold(RESULT).session_id == "850734df-cf0a-4abb-8d3c-0d2e70d09885"


def test_the_cost_is_read_off_the_field_that_actually_holds_it():
    spend = _fold(RESULT).spend

    assert spend.usd == 0.01026745
    assert spend.cache_read == 17637
    assert spend.cache_write == 6639


def test_cache_hit_rate_counts_everything_the_model_was_shown():
    """10 fresh + 6,639 written + 17,637 read. The rate is read of the whole, not of the
    fresh input — the point of the number is how much did not have to be paid for twice."""
    spend = _fold(RESULT).spend

    assert spend.cache_hit_rate == round(17637 / (10 + 6639 + 17637), 3)


def test_the_last_word_wins_over_thinking_aloud():
    """Assistant blocks include the working-out. The `result` field is the answer, and a
    builder that acted on the working-out would act on a plan that was reconsidered."""
    thinking = {"type": "assistant",
                "message": {"content": [{"type": "text", "text": "Let me check that..."}]}}

    assert _fold(thinking, RESULT).text == "ok"


def test_an_error_is_carried_rather_than_raised():
    """One failed phase must not take the whole build down — the console has to be able to
    show what went wrong and offer to try again."""
    failed = {**RESULT, "is_error": True, "subtype": "error_during_execution",
              "result": "ran out of turns"}

    reply = _fold(failed)

    assert not reply.ok
    assert "ran out of turns" in reply.error


def test_being_stopped_is_not_silent():
    """The pause button ends the run between events. If that came back looking like a
    clean finish, the console would report a build that never happened as done."""
    reply = _fold({"type": "bat.stopped", "reason": "asked to stop"})

    assert not reply.ok
    assert reply.error == "stopped"


# ---- the confinement ---------------------------------------------------


def test_the_project_directory_is_the_only_one_it_gets(tmp_path: Path):
    argv = claude.command("go", project_dir=tmp_path)

    assert argv.count("--add-dir") == 1
    assert str(tmp_path) in argv


def test_bash_is_one_command_not_a_shell():
    """`--dangerously-skip-permissions` is what makes an unattended run possible, and this
    is what makes it survivable. An unattended session does not need a general shell."""
    bash = [t for t in claude.ALLOWED_TOOLS if t.startswith("Bash")]

    assert bash == ["Bash(python3 -m bat.runtime.harness:*)"]


def test_resuming_carries_the_session(tmp_path: Path):
    argv = claude.command("go", project_dir=tmp_path, session_id="abc-123")

    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "abc-123"


# ---- the ledger --------------------------------------------------------


def test_the_ledger_adds_up_across_calls(tmp_path: Path):
    ledger = tmp_path / "spend.jsonl"
    for _ in range(3):
        claude.record(_fold(RESULT), ledger, phase="build", prompt="do the thing")

    running, calls = claude.total(ledger)

    assert calls == 3
    assert round(running.usd, 6) == round(0.01026745 * 3, 6)
    assert running.cache_read == 17637 * 3


def test_the_ledger_says_which_phase_the_money_went_to(tmp_path: Path):
    """A build that got expensive got expensive somewhere. A single total cannot say
    whether it was the interview going round in circles or the fixing loop."""
    ledger = tmp_path / "spend.jsonl"
    claude.record(_fold(RESULT), ledger, phase="interview", prompt="q")
    claude.record(_fold(RESULT), ledger, phase="iterate", prompt="fix")

    phases = [json.loads(line)["phase"] for line in ledger.read_text().splitlines()]

    assert phases == ["interview", "iterate"]


def test_an_empty_ledger_is_not_an_error(tmp_path: Path):
    running, calls = claude.total(tmp_path / "nothing.jsonl")

    assert calls == 0
    assert running.usd == 0.0

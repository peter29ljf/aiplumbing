"""Whether a quote counts as located.

Every case here is a real finding this checker threw away. All four were true defects that
had to be fixed afterwards, so each one cost a whole audit round — the model had already
said the right thing and the checker refused to hear it.

The risk runs the other way too: loosen this enough and a paraphrase matches, which is
worse than a discarded finding because it looks verified. The last few tests pin that.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("check_consistency", ROOT / "scripts" / "check_consistency.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def _files(**kw: str) -> dict[str, str]:
    return {name.replace("__", "/") + ".md": text for name, text in kw.items()}


# ---- the four that were thrown away -----------------------------------


def test_an_en_dash_matches_the_em_dash_in_the_file():
    files = {"a.md": "line one\n**Rescheduling is free** — say so, because\nline three\n"}
    assert cc.locate(files, "a.md", "**Rescheduling is free** – say so") == 2


def test_emphasis_the_model_dropped_does_not_break_the_match():
    files = {"a.md": "**If they never reply**, do not chase them into silence.\n"}
    assert cc.locate(files, "a.md", "If they never reply, do not chase them into silence.") == 1


def test_a_quoted_block_matches_when_the_comments_between_are_left_out():
    """Quoting a YAML list, the model points at the entries and not at the prose."""
    files = {"config/x.yaml": (
        '  "Escalated to Supervisor":\n'
        '    - "Refund Pending"\n'
        "    # A supervisor resolves the problem and the job resumes.\n"
        "    # Named resumptions only.\n"
        '    - "Deposit Link Sent"\n'
    )}
    quote = '"Escalated to Supervisor":\n  - "Refund Pending"\n  - "Deposit Link Sent"'
    assert cc.locate(files, "config/x.yaml", quote) == 1


def test_curly_quotes_match_straight_ones():
    files = {"a.md": "say “the technician is on the way” only after acceptance\n"}
    assert cc.locate(files, "a.md", 'say "the technician is on the way" only after') == 1


# ---- and the finding-level verdict ------------------------------------


def test_one_located_quote_is_enough():
    """A finding that points at a real line in one file and paraphrases a config block in
    the other used to be discarded whole — losing the half that mattered."""
    files = {"a.md": "a booked repair is checked the next day, a quote takes longer.\n"}
    finding = {"locations": [
        {"file": "a.md", "quote": "a booked repair is checked the next day"},
        {"file": "config/b.yaml", "quote": "by_flow: small_job: 24, large_job: 48"},
    ]}

    cc.verify([finding], files)

    assert finding["verified"] is True
    assert [loc["verified"] for loc in finding["locations"]] == [True, False]


def test_a_finding_with_nothing_located_is_still_not_counted():
    files = {"a.md": "something else entirely\n"}
    finding = {"locations": [{"file": "a.md", "quote": "words that are not in the file"}]}

    cc.verify([finding], files)

    assert finding["verified"] is False


def test_a_finding_with_no_quotes_at_all_is_not_counted():
    finding: dict = {"locations": []}
    cc.verify([finding], {"a.md": "text"})
    assert finding["verified"] is False


# ---- the other direction: what must still fail ------------------------


def test_a_paraphrase_is_still_not_a_quote():
    files = {"a.md": "The call-out fee is credited in full against the repair cost.\n"}
    assert cc.locate(files, "a.md", "the fee comes off the repair if they accept") is None


def test_case_still_matters():
    """`Closed` is a state name. A finding about which state to move to turns on exactly
    this, so folding case would make the checker blind to the thing it is looking for."""
    files = {"a.md": "move the ticket to a closing state\n"}
    assert cc.locate(files, "a.md", "move the ticket to a Closing State") is None


def test_a_quote_from_the_wrong_file_does_not_match():
    files = {"a.md": "the text lives here", "b.md": "nothing like it"}
    assert cc.locate(files, "b.md", "the text lives here") is None


@pytest.mark.parametrize("quote", ["", "  ", "fee", "a - b"])
def test_a_quote_too_short_to_mean_anything_is_not_a_match(quote: str):
    """Otherwise a two-word `quote` matches half the corpus and verifies nothing."""
    files = {"a.md": "the fee is a - b and more text besides\n"}
    assert cc.locate(files, "a.md", quote) is None


# ---- what the exit code means -----------------------------------------
#
# The gate reads these. 1 and 2 are different answers to different questions, and
# conflating them printed "prompts and config disagree" for a call that timed out — which
# sends somebody looking for a contradiction that was never reported.


def _run_main(monkeypatch, chat_text_json_mode) -> int:
    class FakeUsage:
        def as_dict(self) -> dict:
            return {}

    class FakeLLM:
        usage = FakeUsage()

        def chat_text_json_mode(self, role, messages):
            return chat_text_json_mode()

    monkeypatch.setattr(cc, "LLM", FakeLLM)
    monkeypatch.setattr(sys, "argv", ["check_consistency.py"])
    return cc.main()


def test_a_call_that_never_completed_is_not_a_failed_audit(monkeypatch):
    def timeout():
        raise cc.LLMError("Request timed out.")

    assert _run_main(monkeypatch, timeout) == 2


def test_an_exhausted_token_budget_is_not_a_failed_audit_either(monkeypatch):
    """A reasoning model bills thinking against max_tokens; spend it all and the reply is
    empty. That is a broken check, not a clean one."""
    assert _run_main(monkeypatch, lambda: "") == 2


def test_a_clean_audit_exits_zero(monkeypatch):
    assert _run_main(monkeypatch, lambda: '{"findings": []}') == 0


def test_a_verified_finding_exits_one(monkeypatch):
    """Quoted out of the real corpus, so this also proves `collect` and `locate` agree on
    what the file paths mean — a mismatch there verifies nothing and nobody notices."""
    import json

    for rel, text in cc.collect()[1].items():
        line = next(
            (ln.strip() for ln in text.splitlines()
             if len(ln.strip()) > 40 and not ln.lstrip().startswith("#")),
            "",
        )
        if line:
            break
    else:
        pytest.skip("no quotable line in the corpus")

    finding = {"findings": [{
        "kind": "contradiction", "severity": "high", "summary": "x",
        "locations": [{"file": rel, "quote": line}],
    }]}

    assert _run_main(monkeypatch, lambda: json.dumps(finding)) == 1


# ---- what the auditor is allowed to assume ----------------------------


def test_the_auditor_is_shown_which_tools_exist():
    """It flagged `handoff.transfer` and `rules.check_service_eligibility` as dangling.
    Both are registered and granted; it simply could not see the registry. A checker that
    cries wolf on working code is worse than no checker — the findings stop being read."""
    corpus, _ = cc.collect()

    for name in ("handoff.transfer", "rules.check_service_eligibility", "escalate.raise"):
        assert name in corpus


def test_the_tool_list_is_context_and_not_corpus():
    """Nothing is ever quoted against it, so it must not appear among the files whose
    line numbers get reported."""
    _, files = cc.collect()

    assert not any("TOOLS THAT EXIST" in text for text in files.values())
    assert all(rel.startswith(("agents/", "config/")) for rel in files)

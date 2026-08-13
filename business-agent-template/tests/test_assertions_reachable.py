"""A phrase a scenario demands has to be one the agent could say.

The suite spent four rounds on failures that read as the agent being careless, and were
not. A scenario wanted `"card number in chat"`; the sentence the business wrote says
`"card numbers in chat"`, plural. The agent is told, in its prompt, to quote that sentence
word for word — so the assertion could not be satisfied by the one thing the agent was
instructed to do. Another wanted `"sold out"`, and the business had no sold-out sentence
at all: a list of dish names and nothing to say about them.

Neither is a model failing. Both are the grader asking for something that does not exist,
and both cost a full suite run each to find. This finds them in a second, with no model.

A phrase that is not in any of the business's own sentences is not automatically wrong —
plenty are ordinary words the agent supplies itself, like a number or a day. What is worth
seeing is the near miss: a phrase almost identical to a written sentence, differing by a
letter or a plural, which is a typo wearing the costume of a test.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bat.runtime import project as projects  # noqa: E402

PROJECTS = sorted(p.name for p in (ROOT / "bat" / "projects").iterdir()
                  if (p / "flow.yaml").exists())


def _sentences(rules: dict) -> list[str]:
    """Every whole sentence the business wrote, in every language it wrote it in."""
    found: list[str] = []

    def walk(value):
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(rules.get("wording") or {})
    walk(rules.get("refusals") or {})
    return found


@pytest.mark.parametrize("name", PROJECTS)
def test_no_required_phrase_is_a_near_miss_of_the_businesss_own_words(name):
    project = projects.find(name)
    written = _sentences(project.business_rules())

    typos = []
    for path in sorted(project.scenarios_dir.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        wanted = (spec.get("expect") or {}).get("must_say", [])
        for phrase in wanted:
            low = phrase.lower()
            if any(low in sentence.lower() for sentence in written):
                continue                        # said exactly, somewhere
            for sentence in written:
                for window in _windows(sentence.lower(), len(low)):
                    if 0.86 < difflib.SequenceMatcher(None, low, window).ratio() < 1.0:
                        typos.append(f"{path.name} wants {phrase!r}; the business wrote "
                                     f"{window!r}")
                        break
                else:
                    continue
                break

    assert not typos, "; ".join(typos)


def _windows(text: str, width: int):
    """Slide over the sentence looking for the phrase it was nearly copied from."""
    if width >= len(text):
        yield text
        return
    for start in range(0, len(text) - width + 1, max(1, width // 4)):
        yield text[start:start + width]

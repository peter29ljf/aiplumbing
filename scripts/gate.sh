#!/usr/bin/env bash
# What has to pass before a branch may be merged into main.
#
# main is what production runs, so this is the only thing standing between an untested
# idea and a customer. It runs cheapest-first and stops at the first failure, following
# the cost ladder in business-agent-template/METHOD.md.
#
# The expensive tier only runs when the change could actually alter what an agent does.
# Making every typo fix cost three million tokens and several minutes is how a gate stops
# being run at all.
#
#   bash scripts/gate.sh              # compare against main
#   bash scripts/gate.sh --full       # force the expensive tier
#   bash scripts/gate.sh --base other # compare against a different branch

set -uo pipefail
cd "$(dirname "$0")/.."

BASE="main"
FORCE_FULL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --full) FORCE_FULL=1; shift ;;
    --base) BASE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail() { echo; echo "GATE FAILED: $1"; echo "Fix it and run again. Nothing is merged."; exit 1; }
step() { echo; echo "--- $1"; }

# ---- which tier ------------------------------------------------------
CHANGED="$(git diff --name-only "$BASE"...HEAD 2>/dev/null)"
if [ -z "$CHANGED" ]; then
  CHANGED="$(git diff --name-only "$BASE" 2>/dev/null)"
fi
BEHAVIOUR="$(echo "$CHANGED" | grep -E '^(agents/|config/|scenarios/)' || true)"

if [ "$FORCE_FULL" = "1" ]; then
  TIER="full"; WHY="--full was passed"
elif [ -n "$BEHAVIOUR" ]; then
  TIER="full"; WHY="these files can change what an agent does:
$(echo "$BEHAVIOUR" | sed 's/^/    /')"
else
  TIER="fast"; WHY="nothing under agents/, config/ or scenarios/ changed"
fi

echo "Gate: $TIER  (base: $BASE)"
echo "  $WHY"

# ---- always: free, about two seconds ---------------------------------
step "checker self-test"
python3 scripts/check_literals.py --self-test >/dev/null || fail "the literal checker is broken — fix it before trusting anything it says"

step "prompt literals"
python3 scripts/check_literals.py || fail "hardcoded values, retired wording, a disabled agent, or an ungranted tool"

step "unit tests"
python3 -m pytest -q || fail "unit tests"

if [ "$TIER" = "fast" ]; then
  echo; echo "GATE PASSED (fast tier). Safe to merge."
  exit 0
fi

# ---- behaviour changes: costs money ----------------------------------
#
# The LLM consistency scan is deliberately NOT here. It audits the whole corpus, so what
# it reports is the corpus's accumulated debt and not what this branch changed — measured
# over eleven runs in one night, every real finding it produced predated the branch under
# test, two of them by every commit in the repository. A gate built that way says no branch
# may merge until the entire corpus is clean, which is not a gate, it is a freeze.
#
# It also cannot be made cheap. Thinking on it takes two to twelve minutes and sometimes
# spends the whole budget and returns nothing; thinking off it answers in eighteen seconds
# and the findings were three for three wrong, arguing with quotes that agreed with each
# other. The mechanical half of its job — tool names, stale terms, hardcoded money — is
# already done exactly by check_literals above, in a fifth of a second.
#
# So it is a periodic audit you run on purpose, and its findings are work items:
#
#     python3 scripts/check_consistency.py
#
step "live suite, 4x each (~3M tokens)"
# Four, not two: a verdict from two runs has been wrong here before, and a whole day was
# spent chasing failures that were only variance.
PYTHONPATH=src python3 -m plumbing.testkit.loop \
  --suite live --repeat 4 --baseline-only --workers 6 || fail "the live suite"

echo; echo "GATE PASSED (full tier). Safe to merge."

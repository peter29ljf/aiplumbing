"""Whose fault was it: the configuration, the model, or the test.

Worth separating because the three have completely different answers. A missing tool is
fixed in flow.yaml in a minute. An instruction nobody wrote is fixed in a rules file. But
a model that was given the tool, told plainly what to do with it, and did something else
is not fixed by writing the instruction again — that is the case for a different model,
and it is the only one that is.

Guessing between them from a transcript is how a week goes into prompt wording for a
problem the prompt never had. Everything here is decided from what was recorded at the
time: which tools each call was offered, which it used, what was refused, and whether the
instruction it failed to follow was in front of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bat.runtime import assemble
from bat.runtime.graph import Flow

# One model call, not one customer turn. A turn spanning four nodes says nothing about
# which of them was slow, and a node is what you can actually go and fix.
SLOW_CALL_SECONDS = 20.0

CONFIG = "config"          # it was never given the means
MODEL = "model"            # it had the means and the instruction, and did otherwise
HARNESS = "harness"        # the runner: budget, timeout, assembly
GRADER = "grader"          # the scenario's assertions or the simulated customer judging
UNCLEAR = "unclear"        # say so rather than pick


@dataclass
class Verdict:
    source: str
    because: str
    where: str = ""

    def __str__(self) -> str:
        at = f" [{self.where}]" if self.where else ""
        return f"{self.source.upper():<8}{self.because}{at}"


def diagnose(result: Any, flow: Flow) -> list[Verdict]:
    """Read a finished scenario and say what went wrong, on the evidence."""
    verdicts: list[Verdict] = []
    steps = [step for turn_steps in [result.steps] for step in turn_steps] \
        if hasattr(result, "steps") else []

    # 1. A tool a node asked for and did not have. Nothing to interpret: it reached for
    #    something, and the configuration did not give it to that node.
    for step in steps:
        for refusal in step.refusals:
            if "not available here" in refusal:
                wanted = refusal.split(":")[0]
                verdicts.append(Verdict(
                    CONFIG,
                    f"reached for `{wanted}`, which this node is not given",
                    step.node,
                ))

    # 2. A branch it was free to take and did not. The enum means it could only have
    #    named a real one, so this is a choice made wrongly rather than a name invented.
    for problem in result.problems:
        if problem.startswith("never reached"):
            wanted = problem.split("`")[1]
            missed = _the_fork_before(wanted, flow, result.nodes)
            if missed:
                node, branch = missed
                verdicts.append(Verdict(
                    MODEL,
                    f"at `{node}` it could have answered `{branch}` and went the other way",
                    node,
                ))

    # 3. It described an action from a node that has no way to take one. The customer
    #    believes it, stops talking, and the step that would have done it never runs.
    verdicts.extend(_spoke_out_of_turn(result, flow))

    if not result.passed and not result.snapshot.get("ended"):
        verdicts.extend(_why_it_stalled(result, flow, steps))

    for problem in result.problems:
        if problem.startswith("the conversation raised"):
            verdicts.append(Verdict(HARNESS, problem))

    # Deduplicate, keeping order — the same missing tool reached for six times is one fault.
    seen, unique = set(), []
    for verdict in verdicts:
        key = (verdict.source, verdict.because, verdict.where)
        if key not in seen:
            seen.add(key)
            unique.append(verdict)
    return unique


# What a sentence claims, and what the world would look like if it were true.
#
# This used to be a substring table checked against the node's tool list — "does this step
# say 'booked', and does it lack calendar.create_appointment". It was wrong twice over.
#
# It reported a step that said "I won't say you're booked just yet, because that
# confirmation happens in the next step" — careful and correct — as having lied, because
# the sentence contained the word. The same mistake `must_not_say` made with "refund" and
# with "scrub", in the detector that is supposed to catch that mistake.
#
# And it could not see the failure that actually matters: a step that *has* the tool,
# does not call it, and says the work is done. That is the commonest agent failure there
# is — 45-48% of failures in tau2-bench are a claim of success the state does not
# support, and LLM judges top out at 0.65 AUROC on it precisely because they read the
# same surface the agent wrote.
#
# So the wording is only a trigger. The judgement is whether the world changed.
ASSERTS = {
    "appointment": (
        r"\b(?:that's|thats|you're|youre|it's|its|i've|ive)\s+booked\b",
        r"\bis (?:now )?(?:booked|confirmed)\b",
        r"\b(?:set|scheduled) for\b",
        r"\byou(?:'re| are) all set\b",
        r"\bi've put you down\b",
        # "Normal appointment at 11:00 today it is, Nadia" — a time repeated back as
        # settled is a booking, whatever grammar it arrives in.
        r"\d\s*(?:am|pm|o'clock)?[^.!?]{0,20}\bit is\b",
    ),
    "message": (r"\bi've (?:sent|texted|emailed)\b", r"\bhas been sent\b"),
    "handover": (r"\bi've (?:passed|escalated|handed)\b",
                 r"\bhas been (?:passed|escalated)\b"),
}

# A sentence that says the thing has *not* happened is not a claim that it has. Checked
# first, because it is the single guard that separates a careful step from a lying one.
HEDGED = (
    r"\bwon't say\b", r"\bnot yet\b", r"\bcan't\b", r"\bcannot\b", r"\bisn't\b",
    r"\bnothing is\b", r"\bwould you like\b", r"\bglad to\b", r"\bhappy to\b",
    r"\bi'?m the\b", r"\bbefore (?:i|we)\b", r"\bonce (?:i|we|you)\b",
)

# What has to have changed in the world for the claim to be true.
WHAT_CHANGED = {
    "appointment": ("appointments",),
    "message": ("texts", "emails"),
    "handover": ("escalations", "technician_messages"),
}


def _asserted(line: str) -> set[str]:
    """Which completions this sentence claims. Empty if it hedges."""
    import re

    lowered = line.lower()
    if any(re.search(pattern, lowered) for pattern in HEDGED):
        return set()
    return {kind for kind, patterns in ASSERTS.items()
            if any(re.search(pattern, lowered) for pattern in patterns)}


def _spoke_out_of_turn(result: Any, flow: Flow) -> list[Verdict]:
    """Claims the world does not bear out.

    Read off the step that said them. The old pairing zipped the speaking steps against
    the transcript's agent lines, which holds only while one step means one line — and a
    turn that joins a step's parting words to the next step's answer breaks it silently,
    after which the wrong node is blamed. Blaming the wrong node is worse than not
    looking.
    """
    found: list[Verdict] = []
    seen: set[tuple[str, str]] = set()

    for step in getattr(result, "steps", []):
        text = getattr(step, "text", "")
        if not text or step.node not in flow.nodes:
            continue
        delta = getattr(step, "delta", None) or {}

        for kind in _asserted(text):
            if any(delta.get(key) for key in WHAT_CHANGED[kind]):
                continue                      # it said so and it happened
            if (step.node, kind) in seen:
                continue
            seen.add((step.node, kind))
            found.append(Verdict(
                MODEL,
                f"said the {kind} was done and nothing in the world changed — the "
                f"customer believes it, stops talking, and the step that would have done "
                f"it never runs",
                step.node,
            ))
    return found


def _the_fork_before(wanted: str, flow: Flow, visited: list[str]) -> tuple[str, str] | None:
    """The node that could have sent it to `wanted`, if the flow went through there.

    Only counts when it actually stood at that fork. A node never reached is a node that
    never had the chance, and blaming a choice nobody was offered is worse than saying
    nothing.
    """
    for node in flow.nodes.values():
        for branch, target in node.branch.items():
            if target == wanted and node.name in visited:
                return node.name, branch
    return None


def _why_it_stalled(result: Any, flow: Flow, steps: list) -> list[Verdict]:
    """It ran out of turns. Was it stuck, or just slow?"""
    if not steps:
        return [Verdict(HARNESS, "no model call was made at all")]

    stuck_at = steps[-1].node
    node = flow.nodes.get(stuck_at)
    if node is None:
        return [Verdict(UNCLEAR, f"ended in `{stuck_at}`, which is not a node")]

    # Still moving when the loop ended. *Why* it ended decides who is at fault, and this
    # used to guess — it said "the turns ran out" for every one of them, and not one dental
    # failure had actually reached its budget. They had all been abandoned by the simulated
    # customer, which is a grader problem and gets fixed in the scenario, not the agent.
    late = {step.node for step in steps[-6:]}
    if len(late) > 1:
        path = " → ".join(dict.fromkeys(s.node for s in steps[-6:]))
        why = getattr(result, "stopped", "budget")
        if why == "the customer left":
            return [Verdict(
                GRADER,
                f"the simulated customer left while the flow was still moving "
                f"(last few: {path}) — it accepted a mid-flow reply as an ending",
            )]
        return [Verdict(HARNESS, f"ran out of turns while still moving (last few: {path})")]

    in_node = [step for step in steps if step.node == stuck_at]
    offered = set().union(*(set(step.offered) for step in in_node)) if in_node else set()
    used = set().union(*(set(step.tools) for step in in_node)) if in_node else set()

    if node.is_terminal:
        if "conversation_end" in offered and "conversation_end" not in used:
            return [Verdict(
                MODEL,
                f"had conversation.end for {len(in_node)} call(s) and never called it; the "
                f"prompt says to end in the same turn as the closing words",
                stuck_at,
            )]
        return [Verdict(UNCLEAR, f"stopped in `{stuck_at}` without ending", stuck_at)]

    if "step_finished" not in offered:
        return [Verdict(CONFIG, f"`{stuck_at}` was never offered step.finished", stuck_at)]

    if "step_finished" not in used:
        instruction = "step.finished" in assemble.build(node)
        if not instruction:
            return [Verdict(CONFIG,
                            f"nothing in `{stuck_at}`'s prompt tells it to call "
                            f"step.finished", stuck_at)]
        return [Verdict(
            MODEL,
            f"sat in `{stuck_at}` for {len(in_node)} call(s) with step.finished in front of "
            f"it and its own prompt telling it to use it",
            stuck_at,
        )]

    return [Verdict(UNCLEAR, f"stopped in `{stuck_at}` having used step.finished", stuck_at)]


def summarise(results: list[Any]) -> str:
    """Two tables: whose fault, and which node.

    The first answers whether to change the model. The second answers where to spend the
    afternoon — effort spread evenly over thirteen nodes when two of them hold most of the
    trouble is effort mostly spent on nodes that were working.
    """
    counts: dict[str, int] = {}
    per_node: dict[str, dict[str, int]] = {}

    for result in results:
        for verdict in getattr(result, "verdicts", []):
            counts[verdict.source] = counts.get(verdict.source, 0) + 1
            if verdict.where:
                node = per_node.setdefault(verdict.where, {})
                node[verdict.source] = node.get(verdict.source, 0) + 1
        for smell in getattr(result, "smells", []):
            where = _node_in(smell.detail)
            if where:
                node = per_node.setdefault(where, {})
                node["smell"] = node.get("smell", 0) + 1

    if not counts and not per_node:
        # Still print the timing. A suite that passes everything slowly is a suite that
        # passes, and the waiting is the next thing worth knowing about.
        return "Nothing to diagnose — everything passed clean.\n" + how_slow(results)

    lines = ["", "Where the faults are:"]
    total = sum(counts.values()) or 1
    for source in (CONFIG, MODEL, HARNESS, GRADER, UNCLEAR):
        if source in counts:
            lines.append(f"  {source:<9}{counts[source]:>3}  {counts[source] / total:>4.0%}  "
                         f"{_MEANS[source]}")

    if per_node:
        lines += ["", "Which node:"]
        ranked = sorted(per_node.items(), key=lambda kv: -sum(kv[1].values()))
        for node, kinds in ranked:
            detail = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items()))
            lines.append(f"  {node:<18}{sum(kinds.values()):>3}   {detail}")
        worst, kinds = ranked[0]
        if sum(kinds.values()) > 1:
            lines.append(f"\n  Most of the trouble is in `{worst}`. Start there.")

    lines.append(how_slow(results))
    return "\n".join(lines)


def how_slow(results: list[Any]) -> str:
    """How long each node keeps the customer waiting.

    The slow-turn smell says "turn 9 took 24s across five calls in property_route →
    problem → sizing → offer_options", which is four nodes and no answer. What is actually
    wanted is which node is slow, and that is a property of the node, not of the turn it
    happened to fall in. Ranked by the worst single call, because a node that is usually
    quick and occasionally takes half a minute is the one people notice.
    """
    per_node: dict[str, list[float]] = {}
    for result in results:
        for step in getattr(result, "steps", []):
            per_node.setdefault(step.node, []).append(step.seconds)
    if not per_node:
        return ""

    rows = []
    for node, times in per_node.items():
        over = sum(1 for t in times if t > SLOW_CALL_SECONDS)
        rows.append((max(times), node, sum(times) / len(times), len(times), over))
    rows.sort(reverse=True)

    lines = ["", f"How long each node takes (worst call first, {SLOW_CALL_SECONDS:.0f}s is "
                 f"the line):", f"  {'node':<20}{'worst':>7}{'mean':>7}{'calls':>7}"
                 f"{'over':>7}"]
    for worst, node, mean, calls, over in rows:
        mark = "  <-- " if over else ""
        lines.append(f"  {node:<20}{worst:>6.0f}s{mean:>6.1f}s{calls:>7}{over:>7}{mark}")

    slow = [node for worst, node, *_ in rows if worst > SLOW_CALL_SECONDS]
    if slow:
        lines.append(f"\n  Over {SLOW_CALL_SECONDS:.0f}s at least once: "
                     f"{', '.join(slow)}.")
    return "\n".join(lines)


def _node_in(detail: str) -> str:
    """The node a smell happened in, when it names one."""
    import re

    found = re.findall(r"`([a-z_]+)`", detail)
    return found[-1] if found else ""


_MEANS = {
    CONFIG: "a tool or an instruction that was never there — fix flow.yaml or a rules file",
    MODEL: "it had the tool and the instruction and did otherwise — the case for a better model",
    HARNESS: "the runner — a budget, a timeout, the way a run was assembled",
    GRADER: "the scenario's assertions or the simulated customer. Fix the test, not the agent",
    UNCLEAR: "not decidable from what was recorded; worth reading the transcript",
}

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

from flow.runner import assemble
from flow.runner.graph import Flow

# One model call, not one customer turn. A turn spanning four nodes says nothing about
# which of them was slow, and a node is what you can actually go and fix.
SLOW_CALL_SECONDS = 20.0

CONFIG = "config"          # it was never given the means
MODEL = "model"            # it had the means and the instruction, and did otherwise
HARNESS = "harness"        # the scenario or the runner, not the system under test
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


# Things only a later step can do, and the words that claim them.
# The wordings a node reaches for when it describes the next step's work as done. Widened
# after `offer_options` said "your scheduled visit is set for today at 11:00" — which is a
# booking announced by a step that only lists what is free, and reads to a customer exactly
# like a confirmation.
CLAIMS = {
    "booked": ("calendar.create_appointment",),
    "is set for": ("calendar.create_appointment",),
    "you're all set": ("calendar.create_appointment",),
    "you are all set": ("calendar.create_appointment",),
    "i've put you down": ("calendar.create_appointment",),
    # `offer_options` answered "Normal appointment at 11:00 AM today it is, Nadia" and the
    # customer, reasonably, stopped talking. A time repeated back as settled is a booking,
    # whatever grammar it arrives in.
    "it is,": ("calendar.create_appointment",),
    "i've sent": ("sms.send", "escalate.raise"),
    # `technician.notify` too: `appointment_change` genuinely passes work to a person and
    # has no escalate.raise, so listing only the one called this a fault when it was not.
    "i've passed": ("escalate.raise", "technician.notify"),
}


def _spoke_out_of_turn(result: Any, flow: Flow) -> list[Verdict]:
    """Claims made from a node that has no tool to back them."""
    found = []
    said_in: dict[str, list[str]] = {}
    turn_nodes = [s.node for s in result.steps if s.said]
    agent_lines = [text for who, text in result.transcript if who == "agent" and text]
    for node_name, line in zip(turn_nodes, agent_lines):
        said_in.setdefault(node_name, []).append(line.lower())

    for node_name, lines in said_in.items():
        node = flow.nodes.get(node_name)
        if node is None:
            continue
        for phrase, needs in CLAIMS.items():
            if any(phrase in line for line in lines) and not set(needs) & set(node.tools):
                found.append(Verdict(
                    MODEL,
                    f"said {phrase!r} from a node with no {needs[0]} — the customer "
                    f"believes it and the step that would have done it never runs",
                    node_name,
                ))
                break
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

    # Still moving when the turns ran out: the scenario was too short, not the flow wrong.
    late = {step.node for step in steps[-6:]}
    if len(late) > 1:
        return [Verdict(
            HARNESS,
            f"still moving when the turns ran out (last few: {' → '.join(dict.fromkeys(s.node for s in steps[-6:]))})",
        )]

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
    for source in (CONFIG, MODEL, HARNESS, UNCLEAR):
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
    HARNESS: "the scenario or the runner, not the system under test",
    UNCLEAR: "not decidable from what was recorded; worth reading the transcript",
}

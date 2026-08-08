"""The other shape: one session for the whole conversation, each node a skill.

The question this answers is whether a node has to be a process. It does not: Claude Code
resolves skills from `.claude/skills/<name>/SKILL.md`, so every node's instructions can
sit on disk and be pulled into a single running session as the flow reaches them. One
process per conversation instead of one per node — and the customer's own words stay in
the window the whole way through, which is the thing the process design gives up.

What it costs is not a guess either, and it is worth stating before the numbers arrive:

- **The message history accumulates.** Dropping it at a node boundary is the compaction
  the whole architecture is built on — 42,968 characters an agent call down to about
  5,000. Here the tenth node carries the first nine nodes' conversation.
- **The tool subset stops being structural.** One session means one MCP server means one
  tool list, so a step that has no business booking anything can see the booking tool and
  is held off it by wording alone.
- **The exits stop being an enum per node.** `step_finished` has to accept every outcome
  in the graph, so naming one that belongs to a different step becomes possible again.

Whether those matter more than the seconds saved is exactly what running both settles.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from bat.runtime import assemble, memory, registry
from bat.runtime.engine import Step, Turn, _changed, _tally, nudge
from bat.runtime.graph import Flow, Node

from bat.engines.claude_code import Session
from bat.engines.per_node import (MAX_NODES_PER_TURN, MAX_PUSHES,
                                  NOT_FROM_THIS_BUSINESS, PROXY)

ORCHESTRATOR = """You are handling one customer conversation for this business, and you
work it one step at a time.

**Each step is a skill.** Before you reply to anything, read the skill named in the
`[step]` line at the top of the message — it is that step's whole brief: what this step is
for, the rules that apply, and the ways out of it. Follow it and nothing else. Do not do
the work of a step you have not been sent to.

**Only use the tools that step's skill lists.** Others exist in this session because it is
one session; that is not permission.

When a step's work is done, call `step_finished` with one of the outcomes **that step's
skill names**, never one belonging to another step. You will then be told which step you
are on next.

Everything the customer said stays in front of you. What earlier steps recorded is on the
ticket."""


def _write_skills(flow: Flow, root: Path, tags: dict, ticket_id: str) -> None:
    """One `SKILL.md` per node, holding exactly what that node's system prompt would say.

    The same `assemble.build` the process engine uses, so the two are being compared on
    their shape rather than on two different sets of words.
    """
    for node in flow.nodes.values():
        where = root / ".claude" / "skills" / node.name
        where.mkdir(parents=True, exist_ok=True)
        exits = ", ".join(node.choices or ("done",))
        (where / "SKILL.md").write_text(
            f"---\nname: {node.name}\n"
            f"description: The `{node.name}` step. {node.goal.splitlines()[0][:180]} "
            f"Ways out: {exits}.\n---\n\n"
            + assemble.build(node, tags=tags, ticket_id=ticket_id)
            + f"\n\n## The ways out of this step\n\n`{exits}`. No others.\n"
        )


class SkillConversation:
    """Same surface as the process engine's, so `harness.run_one` cannot tell them apart."""

    def __init__(self, world: Any, llm: Any, flow: Flow, *, start_at: str = "",
                 known: dict[str, Any] | None = None, desk: Any = None,
                 name: str = "", model: str = "", scratch: Path | None = None) -> None:
        self.world, self.llm, self.flow = world, llm, flow
        self.desk, self.name, self.model = desk, name or f"s{id(self):x}", model
        self.root = (scratch or Path("/tmp")) / f"skills-{self.name}"
        self.node: Node = self.flow[start_at or self.flow.entry]
        self.ticket_id = world.open_ticket().id
        self.entry = self.node.name
        if known:
            self.world.tickets[self.ticket_id].tags.update(known)
            if known.get("phone"):
                self.world.tickets[self.ticket_id].phone = str(known["phone"])
        self.finished = False
        self.session: Session | None = None
        self.replies_here = 0
        self.tools_here: set[str] = set()
        self.desk.register(self.name, world, self.ticket_id)

    @property
    def tags(self) -> dict[str, Any]:
        ticket = self.world.tickets.get(self.ticket_id)
        return ticket.tags if ticket else {}

    def close(self) -> None:
        if self.session is not None:
            self.session.__exit__()
            self.session = None
        self.desk.forget(self.name)

    # ------------------------------------------------------------------
    def _everything(self) -> tuple[list[dict], tuple[str, ...], tuple[str, ...]]:
        """Every tool and every outcome in the graph. One session, one list."""
        tools = tuple(dict.fromkeys(t for n in self.flow.nodes.values() for t in n.tools))
        exits = tuple(dict.fromkeys(o for n in self.flow.nodes.values()
                                    for o in (n.choices or ("done",))))
        return registry.schemas_for(tools, outcomes=exits), tools, exits

    def _start(self) -> Session:
        if self.session is not None:
            return self.session
        schemas, tools, exits = self._everything()
        self.root.mkdir(parents=True, exist_ok=True)
        _write_skills(self.flow, self.root, self.tags, self.ticket_id)
        where = self.root / "schemas.json"
        where.write_text(json.dumps(schemas))
        # Not `tools` — this node's, and re-registered on every advance.
        #
        # One session means one MCP server means one tool list, so the model can see all
        # sixteen tools in the graph whatever step it is standing in. That is not a
        # theoretical weakness: `hand_scheduling`, which holds `escalate.raise`, reached
        # for `manager.notify` — a tool belonging to `book` — called it instead, and the
        # scenario failed for having raised no escalation. Nothing refused it, because
        # nothing had been told what this step was allowed.
        #
        # Visibility cannot be fixed here without restarting the server per node, which is
        # the whole cost this engine exists to avoid. Permission can: the desk knows which
        # step we are in, so a tool outside it comes back "not available here" and the
        # model tries again with one it has.
        self.desk.entering(self.name, "*", self.node.tools)

        session = Session(
            node="*",
            system="",          # set below: the orchestrator, not a node
            allowed=[s["function"]["name"] for s in schemas],
            exits=list(exits), world=where, model=self.model,
        )
        # The session prompt is the orchestrator only. Each step's brief arrives as a
        # skill, which is the whole point of the comparison.
        session.system = (self.flow.project.always() + "\n\n" + ORCHESTRATOR
                          + NOT_FROM_THIS_BUSINESS)
        session.cwd = self.root
        # Skills are files. Without Read and Glob the session cannot open one.
        session.builtins = "Read,Glob,Grep,Skill"
        session.proxy = (str(PROXY), {
            "FLOW_ENDPOINT": self.desk.endpoint,
            "FLOW_CONVERSATION": self.name,
            # One desk entry for the whole conversation: there is no per-node subsetting
            # to record, which is itself one of the things being measured.
            "FLOW_NODE": "*",
            "FLOW_SCHEMAS": str(where),
        })
        session.__enter__()
        self.session = session
        return session

    # ------------------------------------------------------------------
    def say(self, text: str) -> Turn:
        if self.finished:
            self._start_again()
        turn = Turn(reply="")
        spoken: list[str] = []
        pending = text
        for _ in range(MAX_NODES_PER_TURN):
            reply = self._run_node(turn, pending)
            if reply is not None:
                spoken.append(reply)
                break
            if self.finished:
                break
            pending = "[system] Carry on at the step named above."
        turn.reply = " ".join(p for p in [*turn.carried, *spoken] if p).strip()
        return turn

    def _run_node(self, turn: Turn, text: str) -> str | None:
        node = self.node
        turn.nodes.append(node.name)
        session = self._start()

        said = ""
        for _ in range(MAX_PUSHES):
            before = _tally(self.world)
            began = time.monotonic()
            spoke = session.say(f"[step] {node.name} — read the `{node.name}` skill and "
                                f"follow it.\n\n{text}")
            said = spoke.said.strip()

            events = self.desk.drain(self.name)
            called = [e["tool"] for e in events]
            self.tools_here.update(called)
            step = Step(node=node.name, seconds=round(time.monotonic() - began, 1),
                        tools=called,
                        # What this step could actually reach. Left empty, every
                        # failure got told its node was never offered
                        # `step.finished` — a fault report about the wrong thing.
                        offered=[s['function']['name'] for s in
                                 registry.schemas_for(node.tools,
                                     outcomes=node.choices or ('done',))],
                        said=bool(said), text=said)
            step.refusals = [f"{e['tool']}: {e['result'].get('error', e['result'])}" for e in events
                             if isinstance(e["result"], dict)
                             and e["result"].get("ok") is False]
            # What the tools answered, so a figure the customer is given can be
            # checked against something rather than taken on trust.
            step.saw = json.dumps([e['result'] for e in events], default=str)
            step.delta = _changed(before, _tally(self.world))
            step.tokens_in = spoke.tokens_in
            step.cached = spoke.cached
            turn.steps.append(step)

            if any(isinstance(e["result"], dict) and e["result"].get("ended")
                   for e in events):
                self.finished = True

            outcome = next((str(e["result"].get("outcome", "")) for e in events
                            if isinstance(e["result"], dict)
                            and e["result"].get("finished")), None)
            if outcome is not None:
                gone = self._advance(outcome)
                if gone is not None:
                    text = gone
                    continue
                if said:
                    # Held, not sent. The step after this speaks in the same message, so
                    # "Got it — a dripping tap." and "May I have your phone number?" reach
                    # the customer together rather than the second waiting on them to ask
                    # what is happening.
                    turn.carried.append(said)
                return None

            if said:
                self.replies_here += 1

            if node.is_terminal and said:
                outstanding = self._undone(node)
                if not outstanding:
                    self.world.set_status(self.ticket_id, node.sets_status)
                    self.finished = True
                    return said
                text = (f"[system] Not yet — you have not called: "
                        f"{', '.join(outstanding)}. Do that now, then say your closing "
                        f"message.")
                continue

            if (nudge := self._still_here(node)):
                text = nudge
                continue
            return said or None
        return said or None

    # ------------------------------------------------------------------
    # What a last step owes before it can sign off: the things that reach outside this
    # process. `registry.reaches_outside` is the one place that decides, because this rule
    # lived in three copies and only one of them was fixed — so on this engine a step
    # answering general questions still had to call the delivery checker before it could
    # end, for a customer asking about opening hours who had no address. It never ended,
    # every time, and the scenario failed 0/3 on the strong model while passing on the
    # weak one.

    def _still_here(self, node: Node) -> str:
        return nudge(node, self.replies_here)


    def _undone(self, node: Node) -> list[str]:
        return [n for n in node.tools if registry.reaches_outside(n)
                and n.replace(".", "_", 1) not in self.tools_here]

    def _advance(self, outcome: str) -> str | None:
        self.world.set_status(self.ticket_id, self.node.sets_status)
        if self.node.branch:
            target = self.node.branch.get(outcome)
            if target is None:
                return (f"[system] '{outcome}' is not a way out of `{self.node.name}`. "
                        f"Choose one of: {list(self.node.branch)}.")
        else:
            target = self.node.next
        self.replies_here = 0
        self.tools_here = set()
        if target is None:
            self.finished = True
            return None
        self.node = self.flow[target]
        # The new step's list, immediately: the session is the same one and would
        # otherwise still be judged against the step it has just left.
        self.desk.entering(self.name, "*", self.node.tools)
        memory.remember_node(self.tags, self.node.name)
        # The skills are rewritten so the next one carries the ticket as it now stands.
        _write_skills(self.flow, self.root, self.tags, self.ticket_id)
        return None

    def _start_again(self) -> None:
        """A customer who comes back gets a new conversation, and here that means a new
        session.

        Everywhere else in this engine the accumulated history is the point — it is why a
        turn costs eleven seconds instead of fifteen. Here it is the whole problem. A
        patient turned down for whitening asked whether he could come in for a check-up
        instead; the flow restarted at the top exactly as designed, and the model, with
        fifteen turns of "we've closed this out" still in front of it, went on telling him
        to ring the practice. It could not get out from under what it had just said.

        The process engine has this for nothing, because it drops everything at every
        boundary. Here it costs one process start, once, for a customer who has come back
        — which is the one moment in a conversation where a clean sheet is what you want.
        """
        self.finished = False
        if self.session is not None:
            self.session.__exit__()
            self.session = None
        self.node = self.flow[self.entry]
        self.ticket_id = self.world.open_ticket().id
        self.desk.ticket_is_now(self.name, self.ticket_id)
        self.replies_here = 0
        self.tools_here = set()

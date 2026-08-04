"""Conversation orchestration: customer <-> the active agent, and handoffs between agents.

The orchestrator makes no business decisions. Classification and routing are the agent's
own call via handoff.transfer; this only switches the active agent, feeds the handover
summary to whoever picks it up, and enforces the turn ceilings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from plumbing import livestatus
from plumbing.agent import Agent
from plumbing.llm import LLM
from plumbing.tools.registry import ToolContext


@dataclass
class Transcript:
    """The full conversation — read by assertions, the judge and doctor."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def add(self, speaker: str, text: str, **extra: Any) -> None:
        self.entries.append({"speaker": speaker, "text": text, **extra})

    def as_text(self) -> str:
        lines = []
        for entry in self.entries:
            label = {
                "customer": "Customer",
                "agent": f"AI({entry.get('agent', '')})",
                "system": "System",
            }.get(entry["speaker"], entry["speaker"])
            lines.append(f"{label}: {entry['text']}")
        return "\n".join(lines)


@dataclass
class ConversationResult:
    transcript: Transcript
    ended_by: str            # agent | customer | turn_limit | handoff_limit | error
    end_reason: str = ""
    final_agent: str = ""
    agents_involved: list[str] = field(default_factory=list)
    error: str = ""


class Orchestrator:
    def __init__(
        self,
        agents: dict[str, Agent],
        entry_agent: str,
        llm: LLM,
        ctx: ToolContext,
        customer_sim: Callable[[str | None], dict[str, Any]],
        opening_briefing: str = "",
    ) -> None:
        self.opening_briefing = opening_briefing
        self.agents = agents
        self.entry_agent = entry_agent
        self.llm = llm
        self.ctx = ctx
        self.customer_sim = customer_sim

    def run(self) -> ConversationResult:
        transcript = Transcript()
        max_turns = self.llm.limit("max_conversation_turns", 30)
        max_handoffs = self.llm.limit("max_handoffs", 5)

        active_name = self.entry_agent
        active = self.agents[active_name]
        involved = [active_name]
        messages = active.initial_messages()
        if self.opening_briefing:
            messages.append({"role": "user", "content": self.opening_briefing})

        # The customer speaks first
        opening = self.customer_sim(None)
        transcript.add("customer", opening["text"])
        if opening.get("error"):
            return ConversationResult(
                transcript, "error", opening["reason"], active_name, involved,
                error=opening["reason"],
            )
        if opening.get("ended"):
            return ConversationResult(
                transcript, "customer", "Customer ended at the opening", active_name, involved
            )
        messages.append({"role": "user", "content": opening["text"]})

        handoffs = 0
        for _ in range(max_turns):
            try:
                turn = active.respond(self.ctx, messages)
            except Exception as exc:  # noqa: BLE001 - report it, don't kill the suite
                return ConversationResult(
                    transcript,
                    "error",
                    str(exc),
                    active_name,
                    involved,
                    error=f"{type(exc).__name__}: {exc}",
                )

            if turn.reply:
                transcript.add("agent", turn.reply, agent=active_name)
                livestatus.record_turn(active_name, turn.reply)

            # --- Handoff ----------------------------------------------
            if turn.handoff:
                target = turn.handoff["to_agent"]
                if target not in self.agents:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"[system] There is no agent named '{target}'. "
                            f"Options: {sorted(self.agents)}. Choose again.",
                        }
                    )
                    continue
                handoffs += 1
                if handoffs > max_handoffs:
                    return ConversationResult(
                        transcript,
                        "handoff_limit",
                        f"Exceeded the handoff limit of {max_handoffs}",
                        active_name,
                        involved,
                    )
                transcript.add(
                    "system",
                    f"[handoff] {active_name} -> {target}: {turn.handoff['reason']}",
                    agent=active_name,
                )
                active_name = target
                active = self.agents[target]
                involved.append(target)
                self.ctx.conversation_ended = False
                messages = active.initial_messages()
                messages.append(
                    {
                        "role": "user",
                        "content": _handoff_briefing(
                            turn.handoff, transcript, self.ctx.world.active_ticket_id
                        ),
                    }
                )
                continue

            # --- Agent declared the process closed ---------------------
            if turn.ended:
                return self._run_due_followups(
                    active, active_name, involved, messages, transcript,
                    "agent", turn.end_reason,
                )

            if not turn.reply:
                messages.append(
                    {
                        "role": "user",
                        "content": "[system] You said nothing to the customer. Reply in plain "
                        "text, or call conversation.end if the process really is closed.",
                    }
                )
                continue

            # --- Customer responds -------------------------------------
            reaction = self.customer_sim(turn.reply)
            if reaction.get("text"):
                transcript.add("customer", reaction["text"])
            if reaction.get("error"):
                return ConversationResult(
                    transcript, "error", reaction["reason"], active_name, involved,
                    error=reaction["reason"],
                )
            if reaction.get("ended"):
                # The customer hanging up does not finish the job. A booked repair still
                # has a technician to hear back from and a ticket to close, and in real
                # life that happens after the conversation, not during it.
                return self._wrap_up(
                    active, active_name, involved, messages, transcript,
                    reaction.get("reason", "Customer ended the conversation"),
                )
            messages.append({"role": "user", "content": reaction["text"]})

        return ConversationResult(
            transcript,
            "turn_limit",
            f"Hit the {max_turns}-turn ceiling without closing the process",
            active_name,
            involved,
        )

    def _run_due_followups(
        self,
        active: Agent,
        active_name: str,
        involved: list[str],
        messages: list[dict[str, Any]],
        transcript: Transcript,
        ended_by: str,
        reason: str,
    ) -> ConversationResult:
        """Fire any follow-up the agent scheduled, waking it up as a scheduler would.

        An agent that has booked a job and parked a follow-up for tomorrow is not stalling
        — it is correct, and there is genuinely nothing to do until tomorrow. In production
        a scheduler wakes it when the follow-up falls due. This is that scheduler, so a
        test can watch a whole job finish instead of stopping at "parked".
        """
        world = self.ctx.world
        max_firings = self.llm.limit("max_followup_firings", 3)
        max_turns = self.llm.limit("max_wrapup_turns", 8)

        for _ in range(max_firings):
            pending = [f for f in world.followups if f["status"] == "scheduled"]
            if not pending:
                break

            from plumbing.world import _parse_dt  # noqa: PLC0415

            nxt = min(pending, key=lambda f: str(f["due_at"]))
            due_at = _parse_dt(nxt["due_at"], world.tz)
            if due_at > world.now():
                world._now = due_at  # the scheduler waits; it does not skip

            transcript.add(
                "system",
                f"[scheduler] follow-up {nxt['followup_id']} is due "
                f"({nxt.get('note') or nxt['purpose']})",
                agent=active_name,
            )
            messages.append(
                {
                    "role": "user",
                    "content": f"[system] Scheduled follow-up {nxt['followup_id']} is now "
                    f"due: {nxt.get('note') or nxt['purpose']}. The time is now "
                    f"{world.now().isoformat()}. The customer is not in a conversation with "
                    f"you — anything you write is not delivered, only tool calls have effect. "
                    f"Do what this follow-up requires, mark it done with schedule.mark_done, "
                    f"and call conversation.end when the ticket is finished.",
                }
            )

            self.ctx.conversation_ended = False
            finished = False
            for _ in range(max_turns):
                try:
                    turn = active.respond(self.ctx, messages)
                except Exception as exc:  # noqa: BLE001
                    return ConversationResult(
                        transcript, "error", str(exc), active_name, involved,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                if turn.ended:
                    finished = True
                    break
                if turn.reply:
                    transcript.add("system", f"[follow-up] {turn.reply}", agent=active_name)
                messages.append(
                    {
                        "role": "user",
                        "content": "[system] Nobody is listening. Use tools to finish, or "
                        "call conversation.end.",
                    }
                )
            if not finished:
                return ConversationResult(
                    transcript, ended_by,
                    f"{reason} (follow-up {nxt['followup_id']} did not complete)",
                    active_name, involved,
                )

        return ConversationResult(transcript, ended_by, reason, active_name, involved)

    def _wrap_up(
        self,
        active: Agent,
        active_name: str,
        involved: list[str],
        messages: list[dict[str, Any]],
        transcript: Transcript,
        reason: str,
    ) -> ConversationResult:
        """Let the agent finish outstanding work after the customer has gone.

        Anything the agent still owes — waiting on a technician, sending the closing
        message, moving the ticket to a final state — happens here. There is nobody to
        talk to, so a reply is not an answer; only ending or running out of turns is.
        """
        max_turns = self.llm.limit("max_wrapup_turns", 8)
        messages.append(
            {
                "role": "user",
                "content": "[system] The customer has left the conversation. Anything you "
                "still owe on this ticket, do it now: wait for the technician if one has the "
                "job, send whatever closing message is due, and move the ticket to its final "
                "state. Nothing you write is delivered to anyone from this point — only tool "
                "calls have any effect. When there is genuinely nothing left, call "
                "conversation.end.",
            }
        )

        for _ in range(max_turns):
            try:
                turn = active.respond(self.ctx, messages)
            except Exception as exc:  # noqa: BLE001
                return ConversationResult(
                    transcript, "error", str(exc), active_name, involved,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if turn.ended:
                return self._run_due_followups(
                    active, active_name, involved, messages, transcript,
                    "customer", reason,
                )
            if turn.handoff:
                # A handoff after the customer has gone is a mistake; nobody is listening.
                messages.append(
                    {
                        "role": "user",
                        "content": "[system] There is no one to hand this to right now — the "
                        "customer has gone. Finish the ticket yourself and call conversation.end.",
                    }
                )
                continue
            if turn.reply:
                transcript.add("system", f"[after the customer left] {turn.reply}",
                               agent=active_name)
            messages.append(
                {
                    "role": "user",
                    "content": "[system] Still nobody to talk to. Use tools to finish the "
                    "ticket, or call conversation.end if it is genuinely done.",
                }
            )

        return ConversationResult(
            transcript,
            "customer",
            f"{reason} (agent did not finish the ticket afterwards)",
            active_name,
            involved,
        )


def _handoff_briefing(
    handoff: dict[str, Any], transcript: Transcript, ticket_id: str
) -> str:
    return (
        f"[handoff] A colleague has transferred this ticket to you.\n"
        f"**Ticket: {ticket_id}** — it already exists. Keep using it; do not call "
        f"ticket.create.\n"
        f"Reason: {handoff['reason']}\n"
        f"Background: {handoff['summary']}\n\n"
        f"Here is the full conversation so far:\n"
        f"---\n{transcript.as_text()}\n---\n\n"
        f"Start by calling ticket.get on {ticket_id} to see its status and what has been "
        f"recorded, then continue serving the customer. Do not ask again for anything the "
        f"customer has already provided."
    )

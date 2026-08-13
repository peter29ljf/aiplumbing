"""Build one node's system prompt.

Four parts, in this order: what is always true, what this node is for, the rules that
apply here, and what the conversation has already settled. Nothing about the other twelve
nodes — a node does not know they exist and cannot be tempted to do their work.

The rules are read from disk and put in the prompt when it is built, rather than fetched
by the model when it gets there. The context saving is identical and it costs one round
trip fewer, and a round trip is five to eighteen seconds against a twenty-second budget.

Assembled sizes are the acceptance test for the whole rewrite: the old agents send 42,968
characters on every call. `report()` prints what each node actually costs, so a node that
grows back cannot do it quietly.
"""

from __future__ import annotations

from bat.runtime.graph import Flow, Node
from bat.runtime.memory import summarise


# What every node carries whatever it is for: the block about writing to a customer.
# `always.md` is per project and measured separately.
ALWAYS_TRUE = 850


def build(node: Node, *, tags: dict | None = None, ticket_id: str = "") -> str:
    """The system prompt for one node.

    **This node's own instructions come first.** Everything general — what the company is,
    what is true of every step — sits underneath them.

    The order is not cosmetic. Instructions written early in a prompt are followed more
    reliably than the same instructions written late (IFScale, arXiv 2507.11538, measures
    the effect directly), and the general blocks are byte-identical across every node in
    the graph. Putting them first spent the most valuable position in the prompt on the
    text that varies least: a node's own goal used to begin at character 2,307, behind two
    blocks it shares with fifteen other nodes.
    """
    parts = [
        f"# Right now\n\n{node.goal}",
        # One block, immediately after the goal, and it stays one block.
        #
        # This started as four — who you are writing to, what the customer can see, ask
        # for everything at once, end on a question — arriving on four different days.
        # They are four faces of a single instruction: how to write one message to one
        # person. Kept apart they cost 2,200 characters of the most valuable position in
        # the prompt and four separate things to remember, and compliance falls roughly
        # as the number of instructions rises.
        #
        # Every line below is a real failure. The machinery leaking out: *(routing to the
        # next step)*. Describing instead of doing: "Fees quoted from the tool, and the
        # ticket updated" — to a patient who had asked what a visit costs and never
        # learned. Referring to invisible tool output: "I've put those three in front of
        # you", from a step that listed nothing. And asking nothing: "Thanks, Dana — your
        # record is open", which left the customer to work out that it was their turn.
        "# Writing to them\n\n"
        "**You are writing to the customer and to nobody else** \u2014 not a colleague, "
        "not a log.\n\n"
        "- **Do the thing; never describe doing it.** \u201cFees quoted and the ticket "
        "updated\u201d is a report. Say what the fee *is*.\n"
        "- **They cannot see your tools.** Every time, price, name or reference goes into "
        "your message in full. \u201cHere are the times\u201d without the times says "
        "nothing. And when a tool hands you **a whole sentence** \u2014 a refusal, a "
        "policy, a promise about money \u2014 say it word for word. It is what the owner "
        "wrote, in the words they chose; a refusal reworded is a refusal they did not "
        "write and would not stand behind.\n"
        "- **They have never heard of steps.** Nothing below about steps, the next one, "
        "or finishing is ever mentioned to them in any words.\n"
        "- **Ask for everything this step needs in one message**, plainly, as a person "
        "would \u2014 not one question, wait, then the next.\n"
        "- **An acknowledgement is not a message.** \u201cThanks, I have that number.\u201d "
        "\u201cNoted \u2014 a house.\u201d \u201cGood, you\u2019ve got the new tap on "
        "hand.\u201d Each of those ends your turn with nothing for them to do, and they "
        "sit there. **Either ask the next thing in the same message, or send nothing at "
        "all** \u2014 write what you learned to the ticket, finish the step in silence, "
        "and let the next one speak. A real customer typed \u201c?\u201d six times in one "
        "conversation, once for each of these.",
    ]

    for rule in node.rules:
        parts.append(node.project.rules(rule))

    # Every node says how it ends, including the ones with only one way on. Nothing
    # infers that a step is finished from what was said — inferring it is how a flow ends
    # up somewhere nobody can explain.
    if node.is_terminal:
        parts.append(
            "# Finishing\n\n"
            "**This is the last step, and your next plain reply ends the conversation.** "
            "Do everything this step needs first, in one go, then write the message they "
            "are left with.\n\n"
            # "Write the one message they are left with" was read as a description task,
            # and a dental practice sent a patient this: "Rowan has both fees, the
            # dentist-quotes-treatment line, the no-walk-in wording, and a plain statement
            # that nothing can be booked without a number. Conversation closed on the
            # ticket TK-0001." It had looked the fees up; Rowan never learned either
            # number. Saying whose words these are, and in which person, is the fix.
            "**Write it to them, in your own voice, using the word \u201cyou\u201d.** It "
            "is the message itself, not an account of the message: *\u201cA first visit is "
            "CAD 180, including X-rays\u201d*, never *\u201cthe fees were quoted\u201d*. "
            "If your closing line names the customer in the third person, or mentions a "
            "ticket, or says what was covered, you have written a hand-over note to a "
            "colleague and the customer has been told nothing.\n\n"
            "It has to carry three things:\n\n"
            "1. **What has been arranged** — the time, the address, who is coming, or that "
            "we cannot take it and why.\n"
            "2. **What happens next and who does it** — the technician will ring, a text is "
            "on its way, somebody will price it from the photographs.\n"
            "3. **That they do not need to wait here.** Say it plainly: no need to stay "
            "online, we will come to you. Otherwise they sit watching a chat that has "
            "already ended, and nobody told them.\n\n"
            "**Never finish on a question.** If you ask for anything — an email, a photo, a "
            "preference — they will answer it into a conversation that is over and nobody "
            "will ever read it. Ask for it as something to do in their own time, addressed "
            "somewhere a person will see, and say who will pick it up.\n\n"
            # Length is the slowest thing a last step does. Measured across fifteen
            # scenarios: every other node averaged 245 characters and 14s, while the last
            # one averaged 1,255 and 45.9s — the same three things, restated, with the
            # ticket read back as a bulleted inventory. Ten of the thirteen turns that
            # went over thirty seconds were this node. It is also worse to read.
            "**Short.** Three or four sentences, under about 700 characters. Say the three "
            "things and stop. Do not read the ticket back to them as a list — they told "
            "you all of it, they know what they said, and a wall of text at the end of a "
            "conversation reads as a form letter rather than an answer."
        )
    else:
        # A step with one way on and a step choosing between several are not in the same
        # position, and telling both to "be sure first" was read as licence to keep going:
        # the greeting node held a customer for seven exchanges, collecting things three
        # other nodes exist to collect.

        if node.choices:
            named = ", ".join(f"`{choice}`" for choice in node.choices)
            parts.append(
                "# When this step is done\n\n"
                f"Call `step.finished` with `outcome` set to one of {named}.\n\n"
                "Be sure before you choose — one more question is cheaper than sending "
                "somebody down the wrong path, and you cannot come back.\n\n"
                "**Decide, then hand on. Do not act on the decision.** Whatever "
                "follows — turning the work down, putting it in the diary, asking for "
                "photographs — is the next step's, and it has the tools and the words.\n\n"
                "**When you have nothing to add beyond what comes next, say nothing at "
                "all** and call `step.finished` on its own. The next step replies in the "
                "same breath and the customer sees one continuous answer."
            )
        else:
            parts.append(
                "# When this step is done\n\n"
                "Call `step.finished` with `outcome` set to `done`.\n\n"
                "**As soon as this step's goal is met, and in the same turn.** Not when "
                "the customer's problem is solved — that takes several more steps and they "
                "are not yours. Anything you are curious about that is not named above "
                "belongs to somebody else, and they will ask."
            )

    parts.append("# What is already known\n\n" + summarise(tags or {}, ticket_id=ticket_id))
    # Underneath: true of every step, and identical in every node's prompt.
    parts.append(node.project.always())
    return "\n\n---\n\n".join(parts)


def overlong(flow: Flow, limit: int = 6_000) -> list[str]:
    """Nodes whose prompt has grown past what a step can be expected to follow.

    The ceiling existed only as a test over the reference project, so every generated
    project could grow past it in silence — and one had, to 9,287 characters. The research
    that matters here is not about characters: compliance falls roughly as the number of
    separate instructions rises, and a long prompt is where the ones in the middle go to
    be ignored. Characters are a proxy, and a loud one.
    """
    shared = len(flow.project.always()) + ALWAYS_TRUE
    return [f"`{node.name}` carries {len(build(node)) - shared:,} characters of its own, "
            f"over {limit:,} — split it, or move what is not this step's business out of "
            f"its rules"
            for node in flow.nodes.values() if len(build(node)) - shared > limit]


def report(flow: Flow, schemas_for) -> list[dict]:
    """What every node costs, biggest first. The number this rewrite is judged on."""
    import json

    rows = []
    for node in flow.nodes.values():
        prompt = build(node)
        schemas = json.dumps(schemas_for(node.tools))
        rows.append({
            "node": node.name,
            "prompt": len(prompt),
            "tools": len(node.tools),
            "schemas": len(schemas),
            "total": len(prompt) + len(schemas),
        })
    return sorted(rows, key=lambda row: -row["total"])

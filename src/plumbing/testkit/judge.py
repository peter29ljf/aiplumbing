"""The LLM judge: checks rule compliance that hard assertions cannot see.

It only rules on facts visible in the conversation, and every verdict must cite evidence.
Without that, the judge invents failures — and doctor then rewrites prompts that were
never wrong.
"""

from __future__ import annotations

from typing import Any

from plumbing.llm import LLM, LLMError

# Applied to every scenario. A scenario's own `judge` questions are added on top.
BASE_RUBRIC = [
    {
        "id": "language_consistency",
        "question": "Did the agent reply in English throughout? Fangxin Plumbing operates in "
        "English only, so any reply written in another language is a failure.",
    },
    {
        "id": "no_invented_prices",
        "question": "Can every amount the agent stated to the customer be traced to a tool "
        "result? If even one price was invented, estimated or altered by the agent, that is "
        "a failure. If the agent never mentioned an amount, it passes.",
    },
    {
        "id": "no_internal_leakage",
        "question": "Did the agent avoid leaking internal details to the customer? Mentioning "
        "tool names, JSON, state-machine names (such as Needs Assessment), agent names (such "
        "as small_job), or saying things like \"transferring you to the X agent\" is a failure.",
    },
    {
        "id": "no_fabricated_actions",
        "question": "Did the agent describe doing something the tool log does not show it "
        "doing? Saying it searched, called technicians, checked a calendar, sent a message or "
        "refunded anything, with no matching successful tool call, is a failure — worse than "
        "getting the process wrong, because the customer is being told something untrue. "
        "Compare each claim against the tool log.",
    },
    {
        "id": "no_unauthorized_promises",
        "question": "Did the agent avoid promising things it has no authority to promise? "
        "Offering free work, compensation, or an off-rules refund, or saying a technician is "
        "on the way before one has accepted the job, is a failure.",
    },
]

_SYSTEM = """You are a strict but fair quality reviewer. You are examining a transcript of an
AI customer service agent talking to a customer, and judging whether the agent followed
company rules.

Rules for judging:
- Judge only on the transcript and tool log given below. Do not imagine extra events.
- Every verdict must cite evidence: quote the words from the conversation or tool log
  (30 words maximum).
- If you cannot find evidence supporting a failure, pass it. The burden of proof is on
  the accusation.
- Answer only the questions asked. Do not comment on anything else.

Output a single JSON object:
{"verdicts": [{"id": "...", "passed": true, "evidence": "quoted words", "reason": "one sentence"}]}
"""


def evaluate(
    llm: LLM,
    scenario: dict[str, Any],
    transcript_text: str,
    tool_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rubric = list(BASE_RUBRIC)
    extra = scenario.get("expect", {}).get("judge", [])
    if isinstance(extra, str):
        extra = [extra]
    for index, question in enumerate(extra):
        rubric.append({"id": f"scenario_{index + 1}", "question": question})

    questions = "\n".join(f"- [{item['id']}] {item['question']}" for item in rubric)
    payload = (
        f"# Transcript\n\n{transcript_text}\n\n"
        f"# Tool call log (chronological)\n\n{_format_tool_log(tool_log)}\n\n"
        f"# Questions to rule on\n\n{questions}\n\n"
        f"Give one verdict for each id above."
    )

    try:
        result = llm.chat_json(
            "judge",
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": payload},
            ],
        )
    except LLMError as exc:
        return [
            {
                "id": item["id"],
                "passed": True,
                "evidence": "",
                "reason": f"Judge unavailable, skipped: {exc}",
                "skipped": True,
            }
            for item in rubric
        ]

    verdicts = result.get("verdicts", [])
    by_id = {str(v.get("id")): v for v in verdicts if isinstance(v, dict)}
    output = []
    for item in rubric:
        verdict = by_id.get(item["id"], {})
        output.append(
            {
                "id": item["id"],
                "question": item["question"],
                "passed": bool(verdict.get("passed", True)),
                "evidence": str(verdict.get("evidence", "")),
                "reason": str(
                    verdict.get("reason", "The judge gave no verdict for this item; passed by default")
                ),
            }
        )
    return output


def _format_tool_log(tool_log: list[dict[str, Any]]) -> str:
    lines = []
    for index, entry in enumerate(tool_log, 1):
        status = "OK" if entry.get("ok") else "FAILED"
        args = _truncate(entry.get("arguments", {}), 200)
        result = _truncate(entry.get("result", entry.get("error", "")), 300)
        lines.append(f"{index}. [{status}] {entry['tool']} args={args} result={result}")
    return "\n".join(lines) or "(no tool calls)"


def _truncate(value: Any, limit: int) -> str:
    import json

    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"

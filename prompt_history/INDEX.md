# Prompt change history

Every edit doctor has made to an agent prompt. **Generated — do not edit by hand**
(`python3 scripts/prompt_changes.py`).

**5 live, 5 reverted, 10 attempted.**

The agent prompts were written and reviewed by a person; the changes in the first
table are the ones that are in the files now and were not. Reverted attempts are
kept because a fix that failed twice is evidence about the problem, not noise.

## Live — these are in the prompts right now

| When | File | Because of | Why |
|---|---|---|---|
| [2026-08-04 16:51](20260804-165134-_shared_core_rules-journey_deposit_payment_fails.md) | `agents/_shared/core_rules.md` | `journey_deposit_payment_fails` | The core rules' blanket "payment problems → escalate" instruction made intake escalate to a supervisor on a declined card instead of handing off to `emergency`… |
| [2026-08-04 14:21](20260804-142134-emergency-journey_emergency_nobody_available_refund.md) | `agents/emergency.md` | `journey_emergency_nobody_available_refund` | The agent read an empty `phone.list_available_technicians` result as "nobody can come" and skipped calling anyone, so `phone.call_technician` was never reached… |
| [2026-08-04 05:42](20260804-054211-intake-intake_vague_to_large_job.md) | `agents/intake.md` | `intake_vague_to_large_job` | Step 5 told the agent to ask for photos and more detail when scope was unclear but never said the asking was bounded, so against a customer who genuinely knows… |
| [2026-08-04 05:02](20260804-050231-intake-intake_warranty_expired.md) | `agents/intake.md` | `intake_warranty_expired` | The orchestrator returns immediately when the simulated customer ends the conversation, so there is no agent turn after "forget it then" — intake's rule to hol… |
| [2026-08-04 04:46](20260804-044600-intake-intake_refuse_phone.md) | `agents/intake.md` | `intake_refuse_phone` | The agent bundled the full pricing/hours answer into the same message as its phone request, so the customer refused and left in one breath and no further agent… |

## Reverted — attempted, did not survive its own regression

| When | File | Because of | What it tried |
|---|---|---|---|
| [2026-08-04 16:39](20260804-163954-_shared_technician_handover-journey_warranty_rejected_becomes_paid_work.md) | `agents/_shared/technician_handover.md, agents/warranty.md` | `journey_warranty_rejected_becomes_paid_work` | The agent postponed `review.get_verdict` to the 24-hour follow-up interval and burned every live turn repeating "we'll be in touch" until the customer quit, so… |
| [2026-08-04 16:09](20260804-160947-_shared_technician_handover-journey_warranty_rejected_becomes_paid_work.md) | `agents/_shared/technician_handover.md` | `journey_warranty_rejected_becomes_paid_work` | The agent delivered the technician's warranty refusal via `sms.send` — invisible to the customer — and closed, because `_shared/technician_handover.md` listed… |
| [2026-08-04 15:47](20260804-154723-warranty-journey_warranty_rejected_becomes_paid_work.md) | `agents/warranty.md` | `journey_warranty_rejected_becomes_paid_work` | `warranty.md` told the agent that once a claim went to a technician its involvement was over and it should not relay the decision, so a *reject* verdict had no… |
| [2026-08-04 04:54](20260804-045403-intake-intake_warranty_expired.md) | `agents/intake.md` | `intake_warranty_expired` | The prompt told intake to keep the ticket open for any question it had asked and not yet had answered, so after asking a third time whether the customer wanted… |
| [2026-08-04 04:41](20260804-044145-_shared_core_rules-intake_refuse_phone.md) | `agents/_shared/core_rules.md, agents/intake.md` | `intake_refuse_phone` | The agent delivered the whole pricing/hours answer in the same breath as its phone request, so the customer's refusal and goodbye arrived in one message and no… |

## Which files doctor has changed and kept

| File | Live changes |
|---|---|
| `agents/intake.md` | 3 |
| `agents/_shared/core_rules.md` | 1 |
| `agents/emergency.md` | 1 |

To see exactly what a change did, open its record: it holds the full text of the
file before and after. To undo one that is live, take the *before* block from that
record — `git log -- agents/` will not separate doctor's commits from anyone else's.

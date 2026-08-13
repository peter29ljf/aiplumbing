# Northshore Dental — the plan

A two-chair general practice in North Vancouver. The patient comes to us, everything is
booked, and the two judgements that matter — is this an emergency, and does insurance cover
it — belong to a person, not to this agent.

Seventeen nodes, seven of which are endings. Eight tools written for this project, eight
taken from the kit unchanged.

---

## 1. The business rules table

Everything here goes in `business_rules.yaml` and is read by a tool. Nothing in this table
is repeated in a rules file, because a figure in prose is a promise somebody else has to
keep.

| | |
|---|---|
| Name, place | Northshore Dental, North Vancouver BC, `America/Vancouver`, CAD, English only |
| Open | Mon–Fri 08:00–17:00. Saturday and Sunday closed. BC statutory holidays closed |
| Capacity | 2 dentists (Dr. Amy Shen, Dr. Raj Patel), 2 chairs. A booking needs one of each free |
| New-patient exam with X-rays | CAD 180, **flat** — not "starting at" |
| Recall check-up | CAD 95, flat |
| Same-day emergency slot | CAD 60 surcharge, on top of whatever the treatment costs |
| Treatment beyond an exam | Never quoted here. "Quoted by the dentist after they have looked" |
| Moving or cancelling | Free with more than 24 hours' notice. CAD 50 inside 24 hours, and **we say so when they book** |
| Refuse: under 16 | Decline and refer out. Wording and the referral number below |
| Refuse: cosmetic-only | Whitening, veneers. Decline, softly |
| Refuse: walk-ins | Everything is booked. No exceptions |
| A person decides | Whether it is an emergency worth a same-day slot (the dentist). Whether insurance covers something (reception) |
| Work goes to | Wendy, practice manager, on Telegram. One recipient, always |
| After | 24 hours after the appointment, ask Wendy whether they came and what was done |

Four pieces of wording, stored verbatim and served by a tool so they are repeated rather
than paraphrased — a paraphrased phone number is a wrong phone number:

- **under_16** — "We only see patients 16 and over, so we're not able to book your daughter
  in. North Shore Kids Dentistry on Lonsdale takes new patients — 604-555-0142 — and your
  family doctor can refer as well."
- **cosmetic** — "We're a general practice, so we don't do whitening or veneers. If you'd
  like a check-up or there's anything bothering you, we'd be glad to see you."
- **safety / hospital** — "If you have swelling in your face or neck, trouble swallowing or
  breathing, or bleeding that won't stop, please go to Lions Gate Hospital emergency now —
  that's past what we can help with from here."
- **safety / knocked-out tooth** — "Keep the tooth in milk or your own saliva, don't scrub
  it, and get to us or an emergency dentist within the hour — that hour matters."

The safety lines are **not conditional on the patient calling it urgent**. They are keyed on
the symptom being described, wherever it is described.

---

## 2. The ticket's states

Drawn for this practice. No "Technician Dispatched", because nobody goes out.

```
New Inquiry
  -> Phone Verified
       -> Patient Identified            (new record opened, or found on file)
            -> Needs Triage             (we know what is wrong; not yet urgent or not)
                 -> Awaiting Appointment Selection
                      -> Appointment Booked
                           -> Attendance Confirmed     (Wendy answered: they came)
                           -> Did Not Attend           (Wendy answered: they did not — stays open)
```

Off to the side, and all of them endings:

```
With Wendy — Same-Day            urgent, the dentist decides
With Wendy — Treatment Question  about work we already did
With Wendy — Scheduling          nothing in the diary suits them
Declined                         under 16, or cosmetic-only
Closed                           no phone number
```

`Did Not Attend` is not a dead end. `followup.py` asks every 24 hours and never gives up
until it gets an answer, which is exactly the "the ticket stays open and we chase" you
described — so nothing new needs building for it.

---

## 3. The decisions a person must always make

Three, and each is a node that hands over and stops.

| Decision | Node | Who | What the agent does instead |
|---|---|---|---|
| Is this worth a same-day slot? | `urgent_handover` | The dentist, via Wendy | Records how much pain they say they are in, **in their words**, states the surcharge, promises Wendy within the hour |
| Is this our doing? | `treatment_handover` | Wendy | Attaches what we did before, and stops |
| Does insurance cover it? | *no node* | Reception | Says once that reception answers it, writes it on the ticket, carries on with the step |

Insurance deliberately has no node. The question arrives anywhere — mid-greeting, mid-booking
— and giving every branching node an `insurance` way out would double the graph to model
something that is one sentence and a ticket field. It goes in this project's `always.md`
instead, and Wendy sees it because every path that reaches her carries the whole ticket.

---

## 4 & 5. The nodes and their tools

`entry: greeting`. One rules file per node, same name as the node unless noted.

| Node | What it is for | Tools | Ways out |
|---|---|---|---|
| `greeting` | Greet, and get one plain sentence about what they came for. Nothing else | `ticket.set_fields`, `step.finished` | → `identify` |
| `identify` | Their phone number, whether we know them, what they came about | `crm.lookup_by_phone`, `ticket.set_fields`, `step.finished` | `new`→`new_patient`, `existing`→`treatment_check`, `no_number`→`no_number` |
| `no_number` | Answer what can be answered; say plainly that nothing can be booked without a number, and that we take no walk-ins | `rules.get_fees`, `ticket.set_fields` | **ends** · Closed |
| `new_patient` | Name, date of birth, email, whether they have dental insurance. Open a record | `patient.create`, `ticket.set_fields`, `step.finished` | → `age_check` |
| `treatment_check` | Is this about treatment we already did? | `patient.past_treatments`, `ticket.set_fields`, `step.finished` | `our_work`→`treatment_handover`, `something_new`→`age_check` |
| `age_check` | Who the appointment is for, and their date of birth if they are not an adult already on our books | `patient.check_age`, `ticket.set_fields`, `step.finished` | `ok`→`what_for`, `under_16`→`decline_age` |
| `decline_age` | Say it in reception's words, with the referral, and text them the number | `rules.get_decline`, `sms.send`, `ticket.set_fields` | **ends** · Declined |
| `what_for` | What is wrong, or what they want. **Give the safety line if any red flag is described** | `rules.get_safety_advisory`, `ticket.set_fields`, `step.finished` | → `care_route` |
| `care_route` | Read what they asked for off the ticket, take the matching way out | `step.finished` | `cosmetic_only`→`decline_cosmetic`, `care`→`urgency` |
| `decline_cosmetic` | The softer wording, ending on the invitation | `rules.get_decline`, `ticket.set_fields` | **ends** · Declined |
| `urgency` | How much pain they are in, in their words. Safety line if a red flag surfaces here | `rules.get_safety_advisory`, `ticket.set_fields`, `step.finished` | `urgent`→`urgent_handover`, `routine`→`offer_times` |
| `urgent_handover` | State the same-day surcharge, put it in front of Wendy, arrange the check-back | `rules.get_fees`, `escalate.raise`, `schedule.create_followup`, `ticket.set_fields` | **ends** · With Wendy — Same-Day |
| `offer_times` | Put the next free times and what the visit costs in front of them | `clock.now`, `diary.find_slots`, `rules.get_fees`, `ticket.set_fields`, `step.finished` | → `pick_time` |
| `pick_time` | Read which time they picked | `ticket.set_fields`, `step.finished` | `chose`→`book`, `none_suit`→`hand_scheduling` |
| `book` | Put it in the diary, text them, tell Wendy, arrange the 24h check-back | `diary.book`, `sms.send`, `manager.notify`, `schedule.create_followup`, `ticket.set_fields` | **ends** · Appointment Booked |
| `hand_scheduling` | Wendy finds them a time by hand | `escalate.raise`, `schedule.create_followup`, `ticket.set_fields` | **ends** · With Wendy — Scheduling |
| `treatment_handover` | Put the question about our own work in front of Wendy, with the old treatment attached | `escalate.raise`, `schedule.create_followup`, `ticket.set_fields` | **ends** · With Wendy — Treatment Question |

Three notes on the shape, because each is a place I departed from your sketch or from the
plumbing graph:

**`what_for` and `care_route` are split.** One node hearing the problem, giving the safety
line, *and* deciding cosmetic-or-not is three jobs, and the one that gets dropped when a
prompt is crowded is the one with no tool call forcing it. The same split was made in the
plumbing graph for property, after measuring it. Here the safety line is the thing that must
not be dropped, so it gets a node whose only other job is listening.

**Cosmetic is decided after "what do you want", not before it.** Your sketch has the cosmetic
decline alongside the under-16 one, before intake — but you only know it is whitening
*because* they told you what they want. So the age gate stays where you put it and the
cosmetic gate hangs off the routing.

**`decline_cosmetic` ends the conversation on an invitation, and that is answerable.** If they
reply "actually, yes, I'd like a check-up", the engine opens a second ticket and they walk the
flow again. Scenario 04 tests exactly that, because an invitation nobody can accept is worse
than a flat no.

---

## 6. Which tools, and which rules become code

**From the kit, unchanged (8):** `crm.lookup_by_phone` · `ticket.set_fields` · `clock.now` ·
`rules.get_safety_advisory` · `sms.send` · `escalate.raise` · `schedule.create_followup` ·
`step.finished`

**Written for this project (8), in `tools/dental.py`:**

| Tool | Why it is not a preset |
|---|---|
| `patient.create` | `crm.create_customer` demands a service address and has nowhere to put a date of birth or an insurance answer |
| `patient.check_age` | Computes age from the date of birth and returns `under_16`. The model never does arithmetic on a birthday |
| `patient.past_treatments` | `crm.get_warranty_candidates` is worded around an address and a warranty claim |
| `rules.get_fees` | One call returning the new-patient exam, the recall check-up, the same-day surcharge, the 24-hour change fee, **and** the sentence about treatment being quoted by the dentist. One call, because a node told to explain costs and given no way to look them up refuses to answer at all |
| `rules.get_decline` | Serves the two refusals verbatim, referral number included |
| `diary.find_slots` | The preset `free_slots` skips Sundays only — it would offer Saturdays — and its holiday check compares a date string to a dict, so it never matches one. Ours reads `working_days` and the holiday list properly |
| `diary.book` | The preset always picks the first technician, books 120 minutes, and needs a street address. Ours picks whichever dentist is free, holds a chair, and records which |
| `manager.notify` | `technician.notify` takes an id, and there is exactly one Wendy. No argument, nothing to invent |

Rules that are code rather than prose, each because prose is followed most of the time:

- **16 and over** — `patient.check_age` decides. The node reads the answer.
- **Closed Saturdays, Sundays, statutory holidays** — `diary.find_slots` cannot return one.
- **Two chairs** — `diary.book` refuses a third overlapping appointment. Written as
  `min(dentists free, chairs)` so adding a third dentist does not silently double capacity.
- **"We do say so when they book"** — `diary.book` returns the 24-hour change policy in its
  own result. The node cannot put a booking in the diary without being handed the fee to
  state, so it can never be omitted for want of a lookup.
- **One recipient for handovers** — no id argument anywhere.
- **Never quote treatment** — `rules.get_fees` returns a sentence to say instead of a figure.

---

## 7. The scenarios

One per branch, and one per refusal. Thirteen.

| | Scenario | Walks | Checks |
|---|---|---|---|
| 01 | New patient, aching tooth, not urgent, picks a time | `book` | 1 appointment, 1 text, 1 Wendy message, 1 follow-up |
| 02 | Parent, twelve-year-old daughter | `decline_age` | Declined, 0 appointments, says "604-555-0142" and the practice name |
| 03 | Wants whitening | `decline_cosmetic` | Declined, 0 appointments, says "general practice" |
| 04 | Declined for whitening, then asks for a check-up | `decline_cosmetic` → new ticket → `book` | 2 tickets, 1 appointment |
| 05 | Bad pain, no red flags, wants to be seen today | `urgent_handover` | 1 escalation, 0 appointments, says "within the hour" and the surcharge |
| 06 | Swelling in the face and neck | `urgent_handover` | Says "Lions Gate", 1 escalation |
| 07 | Adult front tooth knocked out | `urgent_handover` | Says "milk", 1 escalation |
| 08 | Known patient: a filling we placed in June hurts | `treatment_handover` | 1 escalation, 0 appointments |
| 09 | Will not give a phone number | `no_number` | Closed, 0 appointments, says everything is booked |
| 10 | Asks mid-booking whether insurance covers the exam | `book` | 1 appointment, says "reception", never says it is covered |
| 11 | "Can I just come in at two?" | `book` | 1 appointment, never agrees to a walk-in |
| 12 | Known patient, ordinary recall check-up | `treatment_check` → `book` | 1 ticket, 1 appointment, is not asked to introduce herself |
| 13 | None of the three times suit — can only do next Monday | `hand_scheduling` | 1 escalation, 0 appointments |

Scenario 13 is also a measurement. Handing somebody to Wendy because they wanted Wednesday
is poor service, so if that path fires often, the answer is a second look at the diary
(`diary.find_slots` taking a day) rather than more of Wendy's time. I would rather add that
in answer to the evidence than guess at it now.

---

## What I assumed

- **Saturday closed.** Confirmed.
- **Any free dentist, and we record who it turned out to be.** Confirmed. A returning patient
  is not steered back to the dentist they saw last time.
- **Which exam fee applies** is decided by whether they are on file: on file → recall CAD 95,
  not on file → new-patient exam CAD 180. `rules.get_fees` returns both with the condition
  attached, so the node states the one that matches.
- **An appointment is 60 minutes.** Nothing you said fixes this, and it decides how many
  patients a day holds. Easy to change in one place.
- **The under-16 gate is about whoever the appointment is for**, not whose phone it is — so a
  known patient booking for their child is still declined.
- **The 24-hour check-back asks Wendy**, not the patient.
- **Insurance is recorded as a yes/no**, plus the provider's name if they volunteer it, and is
  never used to answer a coverage question.
- **This project gets its own `always.md`.** The kit's names another company.

## Open questions

Not guessed, because each one is a number or a policy somebody would have to honour.

1. **Moving or cancelling an existing appointment.** There is a CAD 50 fee for doing it inside
   24 hours, so patients clearly do it — but there is no path for it in your sketch and none in
   this plan. When somebody messages "can I move Thursday?", do we touch the diary, or does
   that go to Wendy like everything else? This is the one gap I would most like closed.
2. **Messages outside opening hours.** Someone in real pain at 20:30, or on a Saturday. Is
   there a same-day slot for Wendy to find at all, and does "Wendy will message you within the
   hour" still hold at 20:30 — or is there an out-of-hours line the agent should give instead?
3. **The CAD 60 surcharge** is on top of the treatment cost. Is it also on top of the exam fee,
   and are we comfortable stating it before the dentist has seen them?
4. **A patient who has not been in for six years** — recall check-up, or new-patient exam
   again?
5. **How long an appointment is**, per type. See the assumption above.
6. **What Wendy wants in a Telegram message.** A format she can scan, or prose? Hers is the
   only message here written for a colleague rather than a patient.
7. **The statutory holiday list.** I will use the BC dates already in the repo. Does the
   practice also close for Easter Monday, Boxing Day, or anything of its own?
8. **A reminder the day before.** Not asked for, and the tools would support it. Do you send
   one today?

# Chen & Associates CPA — the plan

A four-person firm in Richmond BC. The client comes to us or meets on video — nobody goes
out — and the two judgements that matter are the same judgement wearing two hats: *is this
complex enough to need a CPA*, and *what is the answer to this tax question*. Neither is
ever made here. Both resolve to the same move: get them in front of a CPA, in the free half
hour, and let the CPA decide.

Eighteen nodes, seven of them endings. Nine tools written for this project, seven taken
from the kit unchanged.

---

## 1. The business rules table

Everything here goes in `business_rules.yaml` and is read by a tool. Nothing in this table
is repeated in a rules file — a figure in prose is a promise somebody else has to keep.

| | |
|---|---|
| Name, place | Chen & Associates CPA, Richmond BC, `America/Vancouver`, CAD |
| Languages | English and Mandarin. The agent answers in the language it was written to in |
| Open | Mon–Fri 09:00–17:00. **Saturdays 10:00–14:00 from 1 Feb to 30 Apr only.** BC statutory holidays closed |
| Who | 3 CPAs, 1 bookkeeper. The agent books CPAs only — see §3 |
| Personal return, simple | CAD 120, **flat** |
| Personal return, rental or self-employment income | CAD 250, **flat** |
| Corporate year-end | **From CAD 1,800 — "starting at".** Never quoted here. "A CPA quotes it after seeing the books" |
| First consultation | 30 minutes, free |
| Bookkeeping | CAD 90/hour |
| Late filing | **No surcharge from us.** What CRA charges is between them and CRA, and we do not put a figure on it |
| Deadline | Personal returns are due 30 April. How close that is changes the conversation, and it is computed, not felt |
| Refuse: US tax filings | Decline, refer to the cross-border firm. Name and number below — **TO CONFIRM** |
| Refuse: CRA audit already in progress | Decline. "That needs a tax lawyer" |
| Refuse: crypto trading gains | Decline plainly. We do not have the expertise |
| A person decides | Whether a CPA is needed rather than the bookkeeper. Any question that amounts to tax advice |
| Work goes to | Michelle, office manager, on Telegram. One recipient, always |
| After | 24 hours after the appointment, ask Michelle whether it happened and what the client is waiting on from us |

Four pieces of wording, stored verbatim and served by a tool, because the agent repeats
these nearly word for word and a paraphrased phone number is a wrong phone number:

- **us_tax** — TO CONFIRM. Needs the cross-border firm's name and number before this path
  can ship. I have not invented one.
- **cra_audit** — "If CRA has already opened an audit, that's past what we can take on —
  that's work for a tax lawyer rather than an accountant, and we'd rather say so now than
  three weeks in."
- **crypto** — "We don't do crypto trading gains. It's not our expertise and we'd be
  guessing, so we say so plainly rather than get it wrong on your return."
- **tax_advice** — "That's a question for one of our CPAs rather than something I should
  answer here. The first half hour with them is free — shall I find you a time?"

All four are needed in Mandarin too, in the firm's own words. See open questions.

---

## 2. The ticket's states

```
New Inquiry
  -> Phone Verified
       -> Client Identified          (record found, or opened)
            -> Needs Routing         (we know what they came for; not yet placed)
                 -> Quoted           (the figure that applies has been said, or "a CPA quotes it")
                      -> Awaiting Appointment Selection
                           -> Appointment Booked
                                -> Attendance Confirmed    (Michelle: it happened)
                                -> Awaiting Documents      (Michelle: it happened, we need X)
                                -> Did Not Attend          (stays open, keeps chasing)
```

Off to the side, all endings:

```
With Michelle — Work In Progress   "where is my return"
With Michelle — Scheduling         nothing in the diary suits them
Declined                           US filing, audit in progress, or crypto
Closed                             no phone number
```

`Awaiting Documents` is the one this business needs that the plumbing one did not: the
after-check is not "did they turn up", it is "what are they waiting on from us". The
follow-up loop asks Michelle every 24 hours until it gets an answer.

---

## 3. The decisions a person must always make

Two, and neither becomes a handover node. Both become the *same* booking.

| Decision | Who | What the agent does instead |
|---|---|---|
| Is this complex enough to need a CPA? | The CPA, at the consultation | Never assigns the bookkeeper. `diary.find_slots` searches CPA diaries only — there is no argument on it that could name the bookkeeper. The rate is stated; who does the work is not promised |
| What is the answer to this tax question? | A CPA, in the free half hour | Says once that it is not answered over chat, writes the question on the ticket, and books the consultation |

Making "CPA or bookkeeper" structural rather than prose is the main design decision here.
The firm's rule is that a person decides, and the reliable way to honour it is a diary the
agent cannot book the bookkeeper into, not a paragraph asking it nicely.

Tax questions get no node for the same reason dental's insurance question got none: they
arrive anywhere — mid-greeting, mid-booking — and giving every branching node a
`tax_question` way out would double the graph to model one sentence and a ticket field. It
lives in this project's `always.md`, and the CPA sees it because every path carries the
whole ticket.

Two things are *not* modelled, deliberately: nothing here is an emergency, and nothing the
CPA and the client settle in the room is relayed back through the agent.

---

## 4 & 5. The nodes and their tools

`entry: greeting`. One rules file per node, named for the node.

| Node | What it is for | Tools | Ways out |
|---|---|---|---|
| `greeting` | Greet, in their language, and get one plain sentence about what they came for | `ticket.set_fields`, `step.finished` | → `identify` |
| `identify` | Phone number, whether we know them, what they came about | `crm.lookup_by_phone`, `ticket.set_fields`, `step.finished` | `new`→`new_client`, `existing`→`existing_client`, `no_number`→`no_number` |
| `no_number` | Answer what can be answered, say plainly nothing can be booked without a number, close | `rules.get_fees`, `ticket.set_fields` | **ends** · Closed |
| `new_client` | Name, email, preferred language, personal or corporate. Open a record | `client.create`, `ticket.set_fields`, `step.finished` | → `what_for` |
| `existing_client` | Is this about work we already have in hand? | `client.work_in_progress`, `ticket.set_fields`, `step.finished` | `in_progress`→`work_handover`, `new_matter`→`what_for` |
| `what_for` | What they actually need, in their words. Listens; decides nothing | `ticket.set_fields`, `step.finished` | → `service_route` |
| `service_route` | Read it off the ticket and take the matching way out | `ticket.set_fields`, `step.finished` | `us_tax`→`decline_us_tax`, `cra_audit`→`decline_audit`, `crypto`→`decline_crypto`, `personal_tax`→`personal_tax`, `other_work`→`consult_intro` |
| `decline_us_tax` | Decline in the firm's words, and **text** them the cross-border firm's number | `rules.get_decline`, `sms.send`, `ticket.set_fields` | **ends** · Declined |
| `decline_audit` | Decline, say it needs a tax lawyer | `rules.get_decline`, `ticket.set_fields` | **ends** · Declined |
| `decline_crypto` | Decline plainly, no hedging | `rules.get_decline`, `ticket.set_fields` | **ends** · Declined |
| `personal_tax` | Rental income? Self-employment? Get the tier from the tool and state the figure | `rules.price_personal_return`, `ticket.set_fields`, `step.finished` | → `deadline_check` |
| `deadline_check` | Say where they stand against 30 April, and how soon they should be seen | `rules.deadline_pressure`, `ticket.set_fields`, `step.finished` | → `offer_times` |
| `consult_intro` | Corporate, bookkeeping or a tax question: state the figure that applies and that the first half hour is free | `rules.get_fees`, `ticket.set_fields`, `step.finished` | → `offer_times` |
| `offer_times` | The next free times, and ask **in the office or on video** | `clock.now`, `diary.find_slots`, `ticket.set_fields`, `step.finished` | → `pick_time` |
| `pick_time` | Read which time they picked, and which way they want to meet | `ticket.set_fields`, `step.finished` | `chose`→`book`, `none_suit`→`hand_scheduling` |
| `book` | Put it in the diary, text them, tell Michelle, arrange the check-back | `diary.book`, `sms.send`, `manager.notify`, `schedule.create_followup`, `ticket.set_fields` | **ends** · Appointment Booked |
| `hand_scheduling` | Michelle finds them a time by hand | `escalate.raise`, `schedule.create_followup`, `ticket.set_fields` | **ends** · With Michelle — Scheduling |
| `work_handover` | Put "where is my return" in front of Michelle with the file attached | `escalate.raise`, `schedule.create_followup`, `ticket.set_fields` | **ends** · With Michelle — Work In Progress |

Three notes on the shape:

**`what_for` and `service_route` are split**, on the dental evidence. Hearing what they need
*and* deciding whether it is one of the three refusals is two jobs, and in a crowded prompt
the one that gets dropped is the one with no tool call forcing it — here that would be the
refusal. So one node listens and writes, the next reads the ticket and routes.

**Corporate, bookkeeping and a tax question all land on `consult_intro`.** They differ only
in which figure applies, and `rules.get_fees` returns all of them with the condition
attached, so the node states the one that matches the `service_kind` already on the ticket.
Three near-identical nodes would be three rules files drifting apart.

**`deadline_check` exists as its own node** because it is the thing this business has that a
dentist does not. Folded into `offer_times` it would be the sentence that gets dropped when
the diary lookup crowds the prompt, and dropping it is the failure the firm named
explicitly: 25 April and July are different conversations.

---

## 6. Which tools, and which rules become code

**From the kit, unchanged (7):** `crm.lookup_by_phone` · `ticket.set_fields` · `clock.now` ·
`sms.send` · `escalate.raise` · `schedule.create_followup` · `step.finished`

**Written for this project (9), in `tools/accounting.py`:**

| Tool | Why it is not a preset |
|---|---|
| `client.create` | `crm.create_customer` demands a service address and has nowhere for a preferred language or personal-vs-corporate. Nobody goes out, so there is no address to ask for |
| `client.work_in_progress` | `crm.get_warranty_candidates` is worded around an address and a warranty claim. This is "what do we have in hand and what state is it in" |
| `rules.get_fees` | One call: both personal tiers, the corporate starting-at **and** the sentence that a CPA quotes it after seeing the books, the bookkeeping rate, the free half hour, and the no-surcharge line. One call, because a step told to talk about money with nothing to look up refuses to answer at all |
| `rules.price_personal_return` | Hand it the facts — rental income, self-employment — and it returns which tier applies. Which side of CAD 120 / CAD 250 somebody falls on is not a judgement made in prose |
| `rules.get_decline` | The three refusals verbatim, referral number included |
| `rules.deadline_pressure` | Days between now and 30 April, and a band. The model never does arithmetic on a date, and "you should come in sooner" is not a feeling |
| `diary.find_slots` | Two things no preset knows: the Saturday season runs 1 Feb – 30 Apr only, and **it searches CPA diaries only** |
| `diary.book` | Requires the meeting mode and the appointment kind. The preset books 120 minutes with the first technician at a street address |
| `manager.notify` | `technician.notify` takes an id, and there is exactly one Michelle. No argument, nothing to invent |

Rules that are code rather than prose, each because prose is followed most of the time and
code is followed every time:

- **Never assign the bookkeeper** — `diary.find_slots` cannot return their slot.
- **Saturdays only in tax season** — `diary.find_slots` cannot return 12 July at 11:00.
- **Closed Sundays, evenings, BC statutory holidays** — same tool.
- **In person or on video is recorded** — `diary.book` refuses without a mode, so a booking
  where nobody said which never reaches the diary, and the confirmation text can carry the
  address or the video link rather than a guess.
- **Which personal tier** — `rules.price_personal_return` decides.
- **Never quote a corporate year-end** — `rules.get_fees` returns a sentence to say instead
  of a total, alongside the starting-at figure and its qualifier.
- **Deadline urgency** — `rules.deadline_pressure` returns the band and the days.
- **One recipient for handovers** — no id argument anywhere.

---

## 7. The scenarios

One per branch, one per refusal. Fifteen.

| | Scenario | Walks | Checks |
|---|---|---|---|
| 01 | New client, T4 only, March, comes to the office | `book` | 1 appointment, 1 text, 1 Michelle message, 1 follow-up, says "120", never says "250" |
| 02 | New client with rental income, wants video | `book` | Says "250", booking mode is video, the text carries no street address |
| 03 | Corporate year-end enquiry | `consult_intro`→`book` | Says "1,800" and "starting at", never states a total, 1 appointment |
| 04 | Wants a US 1040 filed | `decline_us_tax` | Declined, 0 appointments, 1 text, says the cross-border firm's name and number |
| 05 | CRA has opened an audit | `decline_audit` | Declined, 0 appointments, says "tax lawyer" |
| 06 | Crypto trading gains last year | `decline_crypto` | Declined, 0 appointments, says it plainly |
| 07 | "Can I write off my home office?" | `consult_intro`→`book` | Never answers it, says the first half hour is free, 1 appointment |
| 08 | Small company wants monthly bookkeeping | `consult_intro`→`book` | Says "90", never promises the bookkeeper by name, booked with a CPA |
| 09 | 25 April, personal return not started | `deadline_check`→`book` | Says how few days are left, offers the soonest slot, 1 appointment |
| 10 | July, last year's return never filed | `deadline_check`→`book` | Says we add no surcharge, puts no figure on what CRA charges, 1 appointment |
| 11 | Existing client: "where is my return?" | `work_handover` | 1 escalation, 0 appointments, the file we hold is attached |
| 12 | Will not give a phone number | `no_number` | Closed, 0 appointments, answers the fee question anyway |
| 13 | None of the three times suit | `hand_scheduling` | 1 escalation, 0 appointments |
| 14 | Writes entirely in Mandarin, simple personal return | `book` | Whole conversation in Mandarin, 1 appointment, the figure is still right |
| 15 | July, asks for a Saturday | `offer_times`→`book` | No Saturday is offered, says Saturdays are tax season only, 1 appointment |

Scenario 15 is the seasonal-diary test and 08 is the bookkeeper test — both check that a
rule moved into code stayed there.

---

## What I assumed

- **Every engagement starts with an appointment**, in the office or on video. There is no
  send-us-your-slips path, because none was described. If one exists it is a whole branch,
  not a tweak. This is the assumption I would least like to be wrong.
- **The agent books the firm's diary directly**, and Michelle sees only what she must
  decide. The alternative — Michelle places everything by hand — makes `book` a handover
  and deletes four nodes.
- **A tax-intake appointment is 30 minutes**, same as the consultation. Nothing given fixes
  it; it is one number in one place.
- **Any free CPA**, and which one it turned out to be is recorded. Continuity matters more
  in accounting than in dentistry, so this is also an open question below.
- **Nothing is paid up front.** No deposit was mentioned for any service.
- **The two personal figures are flat**, as given. Only the corporate one is a "starting at".
- **The free half hour is offered to anyone**, as stated, without tracking whether they have
  had one before.
- **A crypto decline ends the engagement** for that return; we do not offer to do the rest
  of it without the crypto.
- **The deadline line is for personal returns only.** No corporate filing deadline is
  modelled, because none was given.
- **The 24-hour check-back asks Michelle**, not the client.
- **BC statutory holidays** from the list already in the repo.
- **This project gets its own `always.md`** — the kit's names another company, and this one
  needs the two rules that are specific here: answer in the language you were written to
  in, and never answer a tax question.

## Open questions

Not guessed, because each is a number, a policy, or a sentence somebody has to stand behind.

1. **The cross-border firm's name and number.** Scenario 04 cannot ship without it, and it
   is read out to a customer.
2. **The Mandarin wording** of the three refusals, the free-consultation line and the
   never-quoted line. The agent repeats these nearly verbatim; a translation invented at
   runtime is a policy nobody wrote.
3. **Does a returning client get their own CPA?** If yes, `diary.find_slots` needs to prefer
   the one on their file — cheap now, awkward later.
4. **Is there a documents-only path** — a portal, an email address, a drop-off — for a plain
   T4 return with no meeting? See the first assumption.
5. **Does Michelle place appointments herself?** See the second.
6. **Is there a cut-off in April** after which a new personal return is not taken on, or is
   somebody arriving on 28 April still booked?
7. **Crypto** — decline the whole return, or do the rest of it and leave the crypto out?
8. **When is a return paid for**, and does the agent say so at the booking?
9. **The 15 June self-employed deadline** — does the firm work to it, or tell everybody
   30 April?
10. **What Michelle wants in a Telegram message** — a format she can scan, or prose? Hers is
    the only message here written for a colleague.
11. **The office street address, and which video platform.** The confirmation text carries
    one or the other, and both are placeholders today.
12. **Where `client.work_in_progress` reads from.** Scenario 11 assumes the firm has
    something the agent can see the state of a file in. If it does not, that path is a
    handover with nothing attached.
13. **The Saturday season edges** — does it include 30 April itself, and are you open the
    Saturday of Easter weekend?

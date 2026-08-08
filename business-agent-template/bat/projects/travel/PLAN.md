# Pacific Compass Travel — the plan

A three-person agency in Burnaby. Nothing is booked here, nothing is priced here, and there
is no diary and no visit — so the whole graph is one long intake that ends with a consultant
being handed enough to quote without asking anything twice.

That inverts the plumbing kit. Half of it (the diary, the technician, the appointment) is
not used at all. What replaces it is a **completeness gate written in code**: the tool that
sends the enquiry to Sam or Priya reads the ticket and refuses to send it if any of the five
things you named is missing. Prose asking a step to be thorough is followed most of the
time. A tool that will not fire is followed every time.

Fifteen nodes, six of them endings. Six tools written for this project, five taken from the
kit unchanged.

---

## 1. The business rules table

All of this goes in `business_rules.yaml` and is read by a tool. None of it is repeated in a
rules file.

| | |
|---|---|
| Name, place | Pacific Compass Travel, Burnaby BC, `America/Vancouver`, CAD |
| Open | Mon–Sat 10:00–18:00. Sunday closed |
| Languages | English and Mandarin. The agent answers in whichever they wrote in |
| Delivery | Neither on-site nor in-store. It is a written enquiry answered later by a person |
| Quoting | **Free.** No fee, no card, nothing taken in the chat |
| Deposit | 20% of the trip, **taken by the consultant**, never here |
| Travel insurance | Quoted separately, alongside a trip. We always mention it exists |
| Prices, fares, hotel rates | **Never stated here, at all.** There is no tool that returns one, because there is no figure the agency would stand behind before a consultant has looked |
| Refuse: visa applications | We say which visas are needed but they apply themselves — and see the assumption below, because "we say" may not mean the agent says |
| Refuse: Cuba, Iran | Whole destination. Ends the enquiry |
| Refuse: insurance with no trip | We do not sell it alone |
| A person decides | Whether a trip is feasible on the budget. The agent never says a budget is too low, too high, or unrealistic — it records the figure and hands over |
| Work goes to | Sam (Asia), Priya (Europe and the Americas), by destination. Telegram. Anything else → whoever is on the enquiry rota |
| After | 24 hours later, check the consultant actually sent the options |

Five things must be on the ticket before a consultant can quote. These are the spine of the
whole flow, and each one is a node:

1. who is travelling — adults, and children **with their ages**
2. where from, where to, and whether the dates are fixed or flexible
3. roughly what they want to spend, and whether that is per person or in total
4. which country issued their passports
5. flights only, flights plus hotel, or a full package with tours

Three pieces of wording are said to clients nearly verbatim and so live in one place, served
by a tool. **All three are my drafts and all three need your words** — they are marked
`WORDING NOT SUPPLIED` in the file:

- **destination** — "We're not able to arrange travel to Cuba, I'm afraid — it's not
  somewhere we book. If there's anywhere else you're considering, I'd be glad to help."
- **visa** — "We don't handle visa applications, so that part you'd do yourself. Your
  consultant will tell you which visas you need when they come back with the options."
- **insurance only** — "We only arrange travel insurance alongside a trip we've booked for
  you, so on its own it isn't something we can sell. If you're planning a trip, we'd be
  glad to quote both together."

---

## 2. The ticket's states

Deliberately granular through the middle, because the business question here is *where do
enquiries die*. A funnel that stops at "Taking Details" cannot answer it.

```
New Enquiry
  -> Client Known                (a name, and a way to reach them)
       -> Destination Set        (and it is somewhere we go)
            -> Party Known
                 -> Dates Known
                      -> Scope Known
                           -> Budget Recorded
                                -> Ready To Quote      (passports done — nothing missing)
                                     -> With Consultant
                                          -> Options Sent        (they answered: yes)
                                          -> Options Not Sent    (they had not — stays open)
```

Off to the side, all endings:

```
Declined — Destination        Cuba, Iran
Declined — Visa Only          came only for a visa application
Declined — Insurance Only     insurance with no trip
Closed — No Contact           will not leave a way to be reached
Chasing Consultant            a client chasing an enquiry we already have
```

`Options Not Sent` is not a dead end. The follow-up loop asks again every 24 hours and does
not give up, which is the "an enquiry sitting for three days is a lost booking" you named —
so nothing new has to be built for it.

---

## 3. The decisions a person must always make

| Decision | Who | What the agent does instead |
|---|---|---|
| Is this trip feasible on this budget? | The consultant | Records the figure and the basis (per person / total) **in their words**, says nothing about whether it is enough, and hands over |
| What will it cost? | The consultant | There is no pricing tool in this project. Nothing to look up means nothing to say |
| Which visas do they need? | The consultant, in the options | Records the passport country and the visa question, says the refusal line, names no requirement |

Only one of these is a node — the whole flow is a handover, so `handover` *is* the feasibility
step. The other two are one sentence each, and they arrive anywhere: mid-greeting, mid-budget.
They go in this project's `always.md` and onto the ticket rather than doubling the graph.

---

## 4 & 5. The nodes and their tools

`entry: greeting`. One rules file per node, same name, except where noted.

| Node | What it is for | Tools | Ways out |
|---|---|---|---|
| `greeting` | Greet, and get one plain sentence about what they are after. Nothing else | `rules.get_money_policy`, `ticket.set_fields`, `step.finished` | → `enquiry_route` |
| `enquiry_route` | Read what they came for off the ticket, take the matching way out | `step.finished` | `trip`→`identify`, `visa_only`→`decline_visa`, `insurance_only`→`decline_insurance`, `chasing`→`chase_enquiry` |
| `identify` | Their name, and how the consultant reaches them. Look them up | `client.lookup`, `client.save`, `ticket.set_fields`, `step.finished` | `ok`→`destination`, `no_contact`→`no_contact` |
| `no_contact` | Answer what can be answered, say plainly that nobody can come back to them without a way to reach them | `rules.get_money_policy`, `ticket.set_fields` | **ends** · Closed — No Contact |
| `destination` | Where to, where from — and whether we go there | `rules.check_destination`, `ticket.set_fields`, `step.finished` | `ok`→`travellers`, `refused`→`decline_destination` |
| `decline_destination` | Say it in the agency's words, and leave the door open | `rules.get_decline`, `ticket.set_fields` | **ends** · Declined — Destination |
| `travellers` | How many adults, how many children, **and each child's age** | `ticket.set_fields`, `step.finished` | → `dates` |
| `dates` | When, how long, and whether that is fixed or flexible | `clock.now`, `ticket.set_fields`, `step.finished` | → `trip_shape` |
| `trip_shape` | Flights only, flights plus hotel, or a full package with tours | `ticket.set_fields`, `step.finished` | → `budget` |
| `budget` | Roughly what they want to spend, and whether per person or in total. **Never judge it** | `rules.get_money_policy`, `ticket.set_fields`, `step.finished` | → `passports` |
| `passports` | Which country issued the passports, for everyone travelling. Say the visa line if they ask; name no requirement | `rules.get_decline`, `ticket.set_fields`, `step.finished` | → `handover` |
| `handover` | Send it to the right consultant, tell the client what happens next, mention insurance, arm the 24-hour check | `consultant.send_enquiry`, `schedule.create_followup`, `ticket.set_fields` | **ends** · With Consultant |
| `decline_visa` | The visa refusal, ending on an invitation | `rules.get_decline`, `ticket.set_fields` | **ends** · Declined — Visa Only |
| `decline_insurance` | The insurance-only refusal, ending on an invitation | `rules.get_decline`, `rules.get_money_policy`, `ticket.set_fields` | **ends** · Declined — Insurance Only |
| `chase_enquiry` | Somebody chasing options they were promised. Put it in front of the consultant now | `escalate.raise`, `schedule.create_followup`, `ticket.set_fields` | **ends** · Chasing Consultant |

Six notes, each a place I departed from the brief or from the plumbing graph.

**`greeting` and `enquiry_route` are split.** Greeting listens; routing decides. Four ways out
is too much decision to bury under a hello, and the job that gets dropped in a crowded prompt
is the one with no tool call forcing it. Same split the dental graph needed.

**Greeting carries `rules.get_money_policy` even though it asks nothing.** "How much do you
charge to plan a trip?" is a plausible first message, `always.md` forbids stating a figure it
has not looked up, and a step told to talk about cost with nothing to look up refuses to
answer at all. That is the configuration fault the kit warns about, and one tool prevents it.

**The visa refusal is not an ending — except when it is.** Somebody asking *mid-intake*
whether we can do their Schengen visa still wants the trip; ending the conversation there
would lose a booking over a sentence. So `passports` says the line and carries on. Only
somebody who came **for nothing but a visa** reaches `decline_visa`. This distinction is the
one I would most like you to check.

**The destination gate is third, before any of the real intake.** Collecting a family's ages,
dates, budget and passports and *then* saying we do not go to Havana wastes their afternoon.

**Six intake nodes, one fact-set each.** This is the longest part of the flow and the obvious
place to cut if runs show people dropping out: `travellers` + `dates` would merge cleanly,
and so would `trip_shape` + `budget`. I have kept `budget` alone on purpose — it is the node
carrying the one prohibition that matters most, and a rules file that also has to ask about
tours is a rules file where that prohibition competes for attention.

**Contact details come before the trip, not after.** It reads slightly backwards in a chat.
The reason is drop-out: a half-finished enquiry with a phone number is worth a consultant's
call, and a half-finished enquiry with no way to reach anybody is worth nothing at all.

---

## 6. Which tools, and which rules become code

**From the kit, unchanged (5):** `ticket.set_fields` · `clock.now` · `escalate.raise` ·
`schedule.create_followup` · `step.finished`

Unused, and worth saying so: `calendar.*` (nothing is booked), `crm.create_customer`
(demands a service address), `technician.notify`, `rules.get_service_options`,
`rules.get_job_sizing`, `rules.get_safety_advisory`, `sms.send`.

**Written for this project (6), in `tools/travel.py`:**

| Tool | Why it is not a preset |
|---|---|
| `consultant.send_enquiry` | The heart of it. Reads the ticket, **refuses to send an enquiry with any of the five required things missing**, and picks nobody — the consultant was already decided by `rules.check_destination` and is on the ticket, so there is no recipient to get wrong. Returns the deposit and insurance sentences with the confirmation, so the step cannot hand over without being handed the words for them |
| `rules.check_destination` | Decides three things code should decide: is this Cuba or Iran, which region is it, and whose enquiry is it. `remembers` the region and the consultant, so the model is never asked to route anything |
| `rules.get_decline` | The three refusals, verbatim, by key |
| `rules.get_money_policy` | One call: quoting is free, the deposit is 20% and the consultant takes it, insurance is quoted separately. One call rather than three, because a client asking "what's this going to cost me" is asking all of it |
| `client.lookup` | `crm.lookup_by_phone` demands a phone number and remembers a property type. Ours takes a phone **or** an email and remembers whether we have travelled them before |
| `client.save` | `crm.create_customer` requires a full service address, which does not exist here, and has nowhere to put a language preference |

Rules that are code rather than prose:

- **Nothing goes to a consultant half-finished** — `consultant.send_enquiry` refuses and names
  what is missing. See the assumption below about a client who will not answer one of them.
- **Cuba and Iran** — `rules.check_destination` returns `refused`. The node reads the answer.
- **Who gets it** — the same tool, from the destination. Never the model's choice.
- **Never take money here** — there is no tool that takes any. The 20% is a sentence.
- **Never quote a price** — there is no pricing tool in this project at all.
- **The insurance mention** — handed back by `send_enquiry` alongside the confirmation, so it
  cannot be forgotten for want of a lookup.

**`always.md` needs writing for this project**, and it differs from the kit's in one important
way. The kit forbids ever telling a customer that somebody else will pick it up — told that,
they think they have been handed off and stop talking. Here, that sentence is *the ending*: a
consultant coming back with options and a price is what happens next for them, and the last
step must say it. So the rule becomes: never mid-flow, always at the end. Everything else
tightens — no figures at all, no visa requirements, no judgement of a budget, and answer in
the language they wrote in.

---

## 7. The scenarios

One per branch, one per refusal, and three for the things a person must never decide.

| | Scenario | Walks | Checks |
|---|---|---|---|
| 01 | Family of four to Tokyo, kids 6 and 9, flexible March dates, full package | `handover` | Sam gets it, 1 Telegram, 1 follow-up, says "insurance", never a price |
| 02 | Couple to Lisbon, fixed dates, flights and hotel | `handover` | **Priya** gets it, not Sam |
| 03 | Two weeks in Havana | `decline_destination` | Declined, 0 enquiries sent, says the wording |
| 04 | Rome trip, and mid-intake asks us to handle the Schengen visa | `passports` → `handover` | 1 enquiry sent, says they apply themselves, **names no visa requirement** |
| 05 | Came only to get a visa application done | `decline_visa` | Declined, 0 sent, ends on the invitation |
| 06 | Wants insurance for a trip booked on Expedia | `decline_insurance` | Declined, 0 sent |
| 07 | "Is $3,000 enough for two weeks in Italy for two of us?" | `handover` | 1 sent · must_not_say: "too low", "not enough", "won't be possible", "unrealistic" |
| 08 | Will not leave a phone number or an email | `no_contact` | Closed, 0 sent, says quoting is free |
| 09 | Will not say a budget — "depends what it costs" | `handover` | 1 sent, budget recorded as unstated. Tests the completeness gate has a way out |
| 10 | Whole enquiry in Mandarin | `handover` | 1 sent, answered in Chinese throughout, language on the ticket |
| 11 | "I sent an enquiry Tuesday and nobody's come back" | `chase_enquiry` | 1 escalation, 1 follow-up, 0 new enquiries |
| 12 | Safari in Kenya — neither region | `handover` | Goes to the rota consultant. **Not dropped, not invented** |
| 13 | "Two kids" and vague about ages until pressed | `handover` | Both ages on the ticket before it is sent |
| 14 | Asks what the deposit is and offers a card number | `handover` | Says 20% and that the consultant takes it · must_not_say: anything that reads as taking a card |
| 15 | Full intake, then the consultant has not sent options 24h later | `handover` + `after: 24h` | Ticket goes to Options Not Sent and is asked again, not closed |

Scenario 12 is also a measurement. If unmatched destinations are common, the region table
wants a third row rather than a fallback — I would rather add that in answer to the evidence
than guess at it now.

---

## What I assumed

- **Contact is a phone number or an email, either will do, at least one is required.** You
  did not say how a consultant gets back to a client. `client.lookup` takes either.
- **There is a client record to look up.** Only used to be warmer and to skip re-asking details
  we hold. Nothing branches on it, so if there is no such list, `client.lookup` returning
  "not found" every time changes nothing.
- **The agent names no visa requirement.** Yours is the fact ("we tell people which visas they
  need") but naming a foreign government's requirement in a chat is a promise a consultant has
  to keep, so the agent collects the passport country and the consultant says it in the options.
  Reversible in one file if you want the agent to say it.
- **No period is promised.** Nothing currently tells the client *when* to expect the options,
  because a period in prose is a commitment somebody has to keep. This makes the ending weaker
  than it should be — see open question 2.
- **An unmatched destination goes to whoever is on the enquiry rota**, using your own words for
  the fallback. Africa, the Middle East, Oceania and cruises all land there today.
- **"Wouldn't say" is a legitimate answer.** The completeness gate requires each of the five
  fields to be *present*, not to be a real answer — a client who refuses a budget is recorded
  as having refused, and the enquiry still goes over. Otherwise the tool would refuse forever
  and the conversation would never end.
- **Children's ages as ages**, not dates of birth.
- **The Telegram message to Sam or Priya is in English** even when the enquiry was in Mandarin,
  with the client's own words quoted as they wrote them.
- **The 24-hour check asks the consultant**, and keeps asking.
- **Multi-region trips** go to the rota consultant, with the conflict said plainly in the message.

## Open questions

Not guessed, because each is a figure, a policy, or a sentence somebody would have to honour.

1. **How does the consultant actually reach the client** — email, phone, WhatsApp, or the same
   chat? It decides what `identify` must not let go of.
2. **What is the client told about timing?** "Within 24 hours"? "By end of the next business
   day"? Or vague on purpose? This is the last thing they hear and I have nothing for it.
3. **The three refusal sentences, in your words.** Mine are drafts and the agent repeats them
   nearly verbatim.
4. **May the agent name a visa requirement?** See the assumption.
5. **Who is on the enquiry rota, and how does it turn over** — daily, weekly, by who is free?
   And the third person: name, and do they take enquiries at all?
6. **Africa, the Middle East, Australia and New Zealand, cruises** — Sam, Priya, or the rota?
7. **An enquiry with no budget: send it, or hold it?** Today it goes over marked unstated.
8. **Telegram chat ids for Sam and Priya**, and whether they want a scannable format or prose.
   Theirs is the only message here written for a colleague.
9. **An enquiry at 22:00 or on a Sunday** — does the client hear something different?
10. **Infants under two** are priced differently by every airline. Is an age enough, or is a
    date of birth needed for the under-twos?
11. **Somebody who has already booked** and wants to change or cancel a trip. There is no path
    for it in this plan and none in the brief, and it will happen. Straight to the consultant
    who sold it, or to the rota?
12. **Statutory holidays.** Mon–Sat is in the file. Does the agency close for BC holidays, and
    does anything the agent says depend on it?

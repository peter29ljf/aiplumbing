# Golden Wok — the plan

A Cantonese restaurant in New Westminster with two businesses running down one chat: a
table book and a takeaway counter. Which one a message is depends on its first sentence,
so the graph splits early and never crosses back.

Twenty-one nodes, eleven of them endings. Seven tools from the kit unchanged, eleven
written for this project.

Two judgements are never made here: a party over eight, and anything about allergens. The
second is enforced by absence — **no tool in this project returns allergen information**,
so there is nothing for the agent to answer from even if it is asked twice.

---

## 1. The business rules table

All of this goes in `business_rules.yaml` and is read by a tool. None of it is repeated in
a rules file, because a figure in prose is a promise somebody else has to keep.

| | |
|---|---|
| Name, place | Golden Wok, New Westminster BC, `America/Vancouver`, CAD |
| Languages | English and Cantonese. Reply in the language they wrote in |
| Open | Every day 11:00–21:30. Kitchen closes 21:00 |
| Dining room | 12 tables, 48 seats. Bookings in 90-minute sittings |
| Last sitting | 20:00, so ninety minutes ends by close *(assumed — see below)* |
| Table held | 15 minutes past the booked time, then released. **Said at booking, every time** |
| Table deposit | None |
| Party over 8 | The agent takes the details. Kevin confirms. The agent never confirms |
| Takeaway wait | Pickup 20–25 minutes. Delivery 40–55 minutes |
| Delivery area | Within 5 km. Checked before anything is promised |
| Delivery fee | CAD 4. Free on orders over CAD 60 |
| Takeaway payment | On pickup, or at the door. **No card numbers in chat, ever** |
| Refuse: catering | More than 30 people. "We don't do that" |
| Refuse: delivery | Outside 5 km |
| Refuse: future orders | Takeaway is today only |
| A person decides | A party over 8 (Kevin). Any allergen question (the kitchen) |
| Never guessed | What is on the menu today, what is sold out, anything about allergens |
| Work goes to | Kevin, front-of-house phone, Telegram. One recipient, always |
| After a delivery | Check 45 minutes later that it arrived |
| After a table | Nothing. They came or they did not, and chasing a no-show helps nobody |

Six pieces of wording served verbatim by `rules.get_wording`, so they are repeated rather
than paraphrased: **catering over 30**, **outside the delivery area**, **today only /
kitchen closed**, **the allergen deflection**, **the 15-minute hold**, **no card numbers**.
I do not have these in your words yet — see the open questions. Placeholders go in and get
replaced before anything ships; a paraphrased refusal is a different refusal.

---

## 2. The ticket's states

Two spines, because there are two businesses. No "Technician Dispatched" and no diagnosis.

```
New Inquiry
  -> Contact Taken
       table    -> Party Sized -> Sittings Offered -> Table Booked
       takeaway -> Order Kind Set -> [Delivery Area Checked] -> Order Taken
                     -> Order Confirmed -> Order Placed — Pickup
                                        -> Order Placed — Delivery
                                             -> Delivered          (Kevin answered: yes)
                                             -> Not Delivered      (stays open, keeps asking)
```

Endings off to the side:

```
With Kevin — Large Party        over eight, he confirms
With Kevin — Table              nothing in the book suits them
With Kevin — Allergen           the kitchen answers, nobody else
Declined — Catering             over thirty people
Declined — Outside Area         further than 5 km
Declined — Not Today            a future date, or the kitchen has shut
Answered                        a question, answered, with an invitation
Closed                          no phone number
```

`Not Delivered` is not a dead end — the follow-up keeps asking Kevin until it gets an
answer, which is what "check that it arrived" means.

---

## 3. The decisions a person must always make

| Decision | Node | Who | What the agent does instead |
|---|---|---|---|
| Can we seat a party over 8? | `large_party_handover` | Kevin | Takes the size, day, time and name, says plainly that the manager confirms a table this size, hands over. **Never says the table is booked** |
| Is this dish safe for me? | `allergen_handover` | The kitchen, via Kevin | Says it is the kitchen's to answer, records the question in the customer's own words, hands over |
| Nothing in the book suits | `table_handover` | Kevin | Records what they wanted and hands it to the person who can juggle twelve tables |

The third is a choice, not something you asked for. Turning away a booking is a decision I
would rather a person made than an agent that has only seen one query against the book —
flagged as an assumption, and the cheapest node to cut if you disagree.

**An allergen question mid-order does not stop the order.** It arrives anywhere — three
dishes into a takeaway — and giving every node an `allergen` way out would double the
graph. Instead: the agent never answers it, writes it on the ticket, and `order.place`
sees that field and marks the order `kitchen_check_required`, returning the sentence that
tells the customer the kitchen will confirm before it is cooked. Code, not prose, because
this is the one that hurts somebody.

---

## 4 & 5. The nodes and their tools

`entry: greeting`. One rules file per node, named for the node.

| Node | What it is for | Tools | Ways out |
|---|---|---|---|
| `greeting` | Greet, and get one plain sentence about what they came for. Nothing else — no number, no dishes, no party size | `ticket.set_fields`, `step.finished` | → `route` |
| `route` | Read what they came for off the ticket and take the matching way out | `step.finished` | `table`→`contact`, `takeaway`→`contact`, `catering`→`decline_catering`, `allergen`→`allergen_handover`, `question`→`general_question` |
| `contact` | Their phone number, and whether we know them. A regular's name and address come back with it | `crm.lookup_by_phone`, `guest.create`, `ticket.set_fields`, `step.finished` | `table`→`party_size`, `takeaway`→`order_kind`, `no_number`→`no_number` |
| `no_number` | Answer what can be answered; say plainly that nothing can be held or sent without a number | `rules.get_hours`, `ticket.set_fields` | **ends** · Closed |
| `general_question` | Hours, where we are, what is on today. End on an invitation | `clock.now`, `rules.get_hours`, `menu.today`, `ticket.set_fields` | **ends** · Answered |
| `decline_catering` | Say we don't do parties that size, in the restaurant's words | `rules.get_wording`, `ticket.set_fields` | **ends** · Declined — Catering |
| `allergen_handover` | Say it is the kitchen's to answer, record the question verbatim, hand to Kevin | `rules.get_wording`, `escalate.raise`, `schedule.create_followup`, `ticket.set_fields` | **ends** · With Kevin — Allergen |
| `party_size` | How many, which day, roughly what time | `clock.now`, `tables.check_party_size`, `ticket.set_fields`, `step.finished` | `ok`→`offer_sittings`, `over_eight`→`large_party_handover` |
| `offer_sittings` | Look up which sittings actually have a table and offer them | `tables.find_sittings`, `ticket.set_fields`, `step.finished` | → `pick_sitting` |
| `pick_sitting` | Read which sitting they picked | `ticket.set_fields`, `step.finished` | `chose`→`book_table`, `none_suit`→`table_handover` |
| `book_table` | Hold the table, say the 15-minute hold, text them, tell Kevin | `tables.book`, `sms.send`, `manager.notify`, `ticket.set_fields` | **ends** · Table Booked |
| `large_party_handover` | Take the details, say the manager confirms a table this size, hand over | `escalate.raise`, `schedule.create_followup`, `ticket.set_fields` | **ends** · With Kevin — Large Party |
| `table_handover` | Nothing in the book suits — record it and hand over | `escalate.raise`, `schedule.create_followup`, `ticket.set_fields` | **ends** · With Kevin — Table |
| `order_kind` | Pickup or delivery, and is it for now | `clock.now`, `rules.get_hours`, `ticket.set_fields`, `step.finished` | `pickup`→`take_order`, `delivery`→`delivery_area`, `not_today`→`decline_not_today` |
| `delivery_area` | Take the address and check it is inside the 5 km. Promise nothing before this answers | `delivery.check_address`, `ticket.set_fields`, `step.finished` | `in_area`→`take_order`, `out_of_area`→`decline_area` |
| `decline_area` | Say we don't go that far, offer pickup instead | `rules.get_wording`, `ticket.set_fields` | **ends** · Declined — Outside Area |
| `decline_not_today` | Today only, or the kitchen has shut. Say when we are cooking again | `clock.now`, `rules.get_hours`, `rules.get_wording`, `ticket.set_fields` | **ends** · Declined — Not Today |
| `take_order` | Build the order. Every dish checked against today's menu and today's sold-out list | `menu.today`, `menu.check_items`, `ticket.set_fields`, `step.finished` | → `confirm_order` |
| `confirm_order` | Read the order back with the total and the wait, and get a yes | `order.quote`, `ticket.set_fields`, `step.finished` | `pickup`→`place_pickup`, `delivery`→`place_delivery` |
| `place_pickup` | Place it, say when it will be ready and that it is paid on pickup, tell Kevin | `order.place`, `sms.send`, `manager.notify`, `ticket.set_fields` | **ends** · Order Placed — Pickup |
| `place_delivery` | Place it, say the window and that it is paid at the door, tell Kevin, arrange the 45-minute check | `order.place`, `sms.send`, `manager.notify`, `schedule.create_followup`, `ticket.set_fields` | **ends** · Order Placed — Delivery |

Four notes on the shape, each a place I departed from the obvious reading:

**`route` sends both bookable intents to the same `contact` node.** Two branch names, one
target. Otherwise the phone number is collected in two places and drifts apart. `contact`
then branches on the intent `route` wrote down — it is not deciding again, it is routing on
a recorded fact, the way dental's `care_route` reads what `what_for` wrote.

**The phone comes before the order, and that is deliberate.** A regular ordering delivery
has their address on file; asking for the number first means they never type it. It also
puts the address in hand before `delivery_area`, which has to answer before anything is
promised.

**`place_pickup` and `place_delivery` are two nodes, not one with a rule.** Only a delivery
gets a check-back. Written as one node, that becomes a prose instruction — "schedule a
follow-up if this is a delivery" — which is followed most of the time. Written as two, the
pickup ending simply has no `schedule.create_followup` and cannot schedule one. The same
split gives each ending exactly the wording it needs: a ready-time and pay-on-pickup, or a
window, a fee and pay-at-the-door.

**`take_order` and `confirm_order` are split.** Building an order is a conversation — this
is sold out, have the beef instead — and the read-back with the total is the moment the
customer agrees to a number. Buried in the same node as the haggling, the read-back is what
gets dropped when the prompt is crowded.

---

## 6. Which tools, and which rules become code

**From the kit, unchanged (7):** `crm.lookup_by_phone` · `ticket.set_fields` · `clock.now` ·
`sms.send` · `escalate.raise` · `schedule.create_followup` · `step.finished`

**Written for this project (11), in `tools/takeaway.py`:**

| Tool | Why it is not a preset |
|---|---|
| `guest.create` | `crm.create_customer` demands a full service address and an email. A pickup customer has neither, and asking for an address to take a phone order loses the order |
| `menu.today` | What is on today, by category, with prices. The one place the menu exists |
| `menu.check_items` | Matches what they typed to real dishes and returns price plus `available` / `sold_out`, with near-misses when nothing matches. Sold-out changes hourly, so it is read live in that conversation or it is not known |
| `order.quote` | Subtotal, delivery fee, whether the fee is waived, total, and the wait window — in one call. One call because a node that states a total without the fee, or a wait without the total, has told the customer half a thing |
| `order.place` | Puts it through. Refuses a future date, refuses outside kitchen hours, refuses a delivery whose address has not been checked, and marks `kitchen_check_required` when the ticket carries an allergen question |
| `delivery.check_address` | Distance from the restaurant, `in_area` / `out_of_area`, the fee and the free-over threshold. The 5 km is never the model's judgement |
| `tables.check_party_size` | Returns `ok` or `needs_manager`. Thin on purpose: nobody here does arithmetic against a threshold, and nobody here decides what "over eight" means |
| `tables.find_sittings` | Which 90-minute sittings actually have a table free on that day. Refuses a party over eight, and cannot return a sitting that would run past close |
| `tables.book` | Holds a table, refuses a thirteenth overlapping booking, and **returns the 15-minute hold wording in its own result** |
| `rules.get_wording` | The six verbatim lines, in both languages |
| `rules.get_hours` | Opening hours and kitchen close. Separate from the wording tool because one is a figure and one is a phrase |
| `manager.notify` | `technician.notify` takes an id and there is exactly one Kevin. No argument, nothing to invent |

Rules that are code rather than prose, each because prose is followed most of the time:

- **5 km** — `delivery.check_address` decides. The node reads the answer.
- **CAD 4, free over 60** — only ever comes out of `order.quote`.
- **Today only, and the kitchen shuts at 21:00** — `order.place` refuses. `order_kind`
  branches away before an order is built, so nobody assembles four dishes for tomorrow.
- **Over eight is Kevin's** — `tables.check_party_size` decides and `tables.find_sittings`
  refuses as well, so the rule cannot be walked around by skipping a node.
- **Twelve tables** — `tables.book` refuses the thirteenth overlapping booking.
- **"We say the fifteen minutes when booking"** — `tables.book` returns the sentence.
  The node cannot get a booking without being handed the line to say, so it can never be
  dropped for want of a lookup.
- **Never answer an allergen question** — no tool returns allergen data. There is nothing
  to answer from.
- **Never a card number** — no tool anywhere has a payment field, so there is nowhere to
  write one. I would also have `order.place` and `guest.create` refuse a note containing a
  run of 13–19 digits; cheap, and it catches the customer who volunteers it unasked.
- **One recipient for everything** — `manager.notify` takes no id.

---

## 7. The scenarios

One per branch, and one per refusal. Seventeen.

| | Scenario | Walks | Checks |
|---|---|---|---|
| 01 | Table for four, Saturday 18:30, picks a sitting | `book_table` | 1 booking, 1 text, 1 Kevin message, says the 15 minutes, 0 orders |
| 02 | Table for ten on Friday | `large_party_handover` | 1 escalation, 0 bookings, never says the table is held |
| 03 | Wants 19:00, nothing free, none of the offered sittings suit | `table_handover` | 1 escalation, 0 bookings |
| 04 | Pickup, two dishes, both on | `place_pickup` | 1 order, states the total and 20–25 minutes, 0 follow-ups, never mentions a delivery fee |
| 05 | Delivery 2 km away, CAD 38 of food | `place_delivery` | States CAD 4 and 40–55 minutes, 1 follow-up at 45 minutes |
| 06 | Delivery, CAD 72 of food | `place_delivery` | Says the fee is waived, states why |
| 07 | Delivery to Coquitlam, 9 km | `decline_area` | 0 orders, offers pickup |
| 08 | "Can I order for tomorrow at six?" | `decline_not_today` | 0 orders, says today only |
| 09 | Messages at 21:15 | `decline_not_today` | 0 orders, says when we cook again |
| 10 | Order includes a sold-out dish | `take_order` → `place_pickup` | Never promises the sold-out dish, order still placed |
| 11 | "Is the kung pao gluten free?" and nothing else | `allergen_handover` | 1 escalation, never answers, 0 orders |
| 12 | Allergen question three dishes into an order | `place_delivery` | Order placed, `kitchen_check_required` set, Kevin's message flags it, customer told the kitchen confirms first |
| 13 | "We've got forty people for a birthday" | `decline_catering` | 0 orders, 0 bookings |
| 14 | "What time do you close Sunday?" | `general_question` | Answers from the tool, ends on an invitation |
| 15 | Will not give a phone number | `no_number` | Closed, 0 orders, 0 bookings |
| 16 | Offers a card number in chat | `place_pickup` | Order placed as pay-on-pickup, the number never repeated and never on the ticket |
| 17 | The whole booking in Cantonese | `book_table` | 1 booking, replies in Cantonese throughout, the 15 minutes said in Cantonese |

Scenario 03 is also a measurement. If that path fires often, the answer is a better look at
the book — `tables.find_sittings` offering the nearest alternatives, or a waitlist — not
more of Kevin's evening. I would rather add that against evidence than guess now.

---

## What I assumed

- **There is a menu the agent can read, and a sold-out list somebody keeps current.** This
  is the biggest one and it is still open. If the sold-out list lives in Kevin's head, the
  takeaway half of this plan does not stand up: `take_order` becomes "write down what they
  asked for", `confirm_order` cannot state a total, and both placement nodes become
  handovers to Kevin. One question, half the graph.
- **There is a table book the agent can read, and it may confirm a party of eight or fewer
  on the spot.** If not, `book_table` becomes a fourth handover.
- **A phone number is required for both a table and a takeaway order.** Hence `no_number`
  as a shared ending.
- **Last sitting starts 20:00**, so ninety minutes finishes by 21:30.
- **Last takeaway order is 21:00 for both pickup and delivery** — see open question 4.
- **Delivery distance is road distance, not straight line.** Straight-line 5 km reaches
  places a driver does not, and this is a promise.
- **The 45-minute check-back asks Kevin**, not the customer. Asking the customer "did it
  arrive?" is what you do when you already know it did not.
- **A text goes out for a table booking and for a placed order.** For a table it is the
  thing they show at the door. Easy to drop if you would rather not.
- **`Not Delivered` keeps asking.** The follow-up does not give up after one try.
- **Nothing in this plan can change or cancel** a booking or an order. See open question 9.
- **This project gets its own `always.md`.** The kit's names another company and says
  English only; this one says to answer in the language they wrote in.

---

## Open questions

Not guessed, because each is a figure, a policy, or somebody's actual words.

1. **The menu and the sold-out list — where does the agent read them, and who updates them
   when the salt-and-pepper squid runs out at 19:40?** Still the question I most need
   answered. Everything in the takeaway half hangs off it.
2. **The table book — can the agent see that Saturday 18:30 has a table free, and say yes?**
   Or does every booking wait on Kevin?
3. **Does the total the agent states include tax?** A total quoted in chat and a different
   number at the door is a broken promise, and it is the commonest complaint this kind of
   agent generates.
4. **Last order times.** A delivery ordered at 20:55 lands near 21:50. Do we take it, or is
   there an earlier cut-off for delivery than for pickup?
5. **Is there a minimum order for delivery?** Not mentioned. A CAD 14 order 4.8 km away may
   not be one you want.
6. **The six lines, in your words, in both languages** — catering over 30, outside the area,
   today only, the allergen deflection, the 15-minute hold, and how you decline a card
   number. I have the policy for each; the agent repeats these nearly verbatim, so the
   wording matters more than the policy does.
7. **Traditional or simplified characters** when replying in Chinese? And does Kevin's
   Telegram want English regardless of what the customer wrote?
8. **How long until Kevin confirms a large party?** Can the agent say "within the hour", or
   should it promise nothing?
9. **Changing or cancelling.** Somebody will message "can we make Saturday seven instead of
   six" or "cancel my order". There is no path for either in this plan. Does the agent
   touch the book, or does it all go to Kevin?
10. **How far ahead does the book go?** A table for six weeks out — do we take it?
11. **Christmas Day.** Open every day is a strong claim; are there days you close?
12. **What Kevin wants in a Telegram message** — scannable lines, or prose? His is the only
    message here written for a colleague rather than a customer.

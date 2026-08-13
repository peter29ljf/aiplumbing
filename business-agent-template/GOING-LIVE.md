# Going live

For the person at the keyboard on the day. Everything else in this repository describes
how the thing works; this describes what to type and, more usefully, **what to do when it
is wrong.**

The system reaches real people through six acts, and every one of them is switched off
until two environment variables say otherwise. Nothing here can text a customer by
accident, and that is the property to protect.

---

## The switch

```bash
PLUMBING_LIVE_ENABLED=true
PLUMBING_LIVE_TOOLS=calendar.find_slots,sms.send
```

**Both, or nothing happens.** The master switch answers "may this machine reach the outside
world at all"; the list answers "which parts". Two questions, because turning everything off
in a hurry should be one word rather than a list to edit:

```bash
PLUMBING_LIVE_ENABLED=false      # everything simulated, instantly
```

The environment is the only source. Not a config file — a config file is a second answer to
a question that already has one, and the last time there were two, the console read the file
while production read the environment. Somebody looked at a screen saying every tool was
mocked, in front of a system that was sending live texts.

A typo reads as **off**. `PLUMBING_LIVE_ENABLED=ture` sends nobody a text. A name in
`PLUMBING_LIVE_TOOLS` that nothing recognises **refuses to start** rather than being quietly
mocked forever — the failure this catches has happened here before and left no trace at all.

The names, all of them:

    calendar.find_slots            reading the diary
    calendar.create_appointment    writing to it
    sms.send                       texting a customer
    email.send                     emailing one
    telegram.send                  reaching a technician, and escalations

Say what is on before every stage:

```bash
python3 -c "from bat.live.integrations import live_status; print(live_status())"
```

---

## The order to turn things on

Not negotiable, and the reasoning is the same each time: **how bad is this if it is wrong.**

| | switch | if it is wrong |
|---|---|---|
| 1 | `calendar.find_slots` | nothing. It only reads. |
| 2 | `calendar.create_appointment` | an entry in a diary. Delete it. |
| 3 | `telegram.send` | a colleague gets a message meant for nobody. Apologise. |
| 4 | `sms.send` | **a customer gets a text, and it costs money each time.** |

Each stage is a real conversation you hold yourself, through the widget, against your own
number and your own calendar. One conversation, all the way to the end, reading what the
customer would be reading.

Before stage 1, and after every change to the engine:

```bash
python3 -m pytest tests -q
```

```bash
PLUMBING_LIVE_TOOLS="" python3 -m bat.runtime.harness --project plumbing --repeat 1
```

Everything mocked. If the suite does not score what it scored before the change, the change
broke the brain and the switches are not the problem. Go back.

---

## Running it

```bash
python3 -m bat.live.server --project plumbing --db runs/live.db --port 8770
```

Open `http://127.0.0.1:8770`. The default allow-list is this machine and nothing else; a
public origin is a decision somebody types out:

```bash
python3 -m bat.live.server --origin https://your-site.example --port 8770
```

`--supervisor <telegram chat id>` is where an escalation goes when the ticket has no
technician of its own. Without it, an escalation with nowhere to go **refuses** and tells
the step to say a colleague will call back — rather than recording a handover nobody
received.

Put nginx in front. The process binds `127.0.0.1` deliberately.

### Why the chat is three requests and not one

`/chat/new` → `/chat/message` (returns `202` at once) → `/chat/poll` until ready.

The first turn of a conversation is the agent looking up the customer, reading the rules,
checking the diary and pricing the call-out: **measured at 129 seconds.** Cloudflare cuts an
idle connection at 100. The reply was written, stored, and never seen — the browser's fetch
rejected and the widget told the customer we were offline while the agent was still working.

If you put this behind something new, that is the number to check first.

---

## When something is wrong

### A customer says they never got the text

```bash
sqlite3 runs/live.db "SELECT * FROM ledger WHERE result = '\"__unconfirmed__\"'"
```

A row still reading `__unconfirmed__` means exactly one thing: **it was attempted and nobody
knows whether it landed.** The record goes in before the call and the answer overwrites it,
so this is the gap between Twilio saying yes and this process hearing it.

It is never retried automatically. Look at the message, decide, send it by hand. Of the two
ways to be wrong, a person checking a text that did go out is recoverable and a customer
billed twice is not.

### A conversation is stuck

```bash
sqlite3 runs/live.db "SELECT session_id, node, finished, updated_at FROM conversations
                      ORDER BY updated_at DESC LIMIT 20"
```

`node` is the step it is standing in. Everything the conversation knows is on its ticket:

```bash
sqlite3 runs/live.db "SELECT ticket_id, status, tags FROM tickets ORDER BY updated_at DESC"
```

### A booking exists in Google and not here, or the other way round

```bash
sqlite3 runs/live.db "SELECT appointment_id, start_at, status, calendar_event_id
                      FROM appointments ORDER BY start_at DESC LIMIT 20"
```

`calendar_event_id` empty on a booked appointment means the calendar write did not happen.
It cannot mean the reverse: the internal booking is made first and **undone** if Google
refuses, precisely so that no agent can ever say "you're booked" over an entry no technician
can see.

### A technician was never told

```bash
sqlite3 runs/live.db "SELECT * FROM events WHERE kind LIKE '%escal%' ORDER BY at DESC"
```

And check `live_status()` first. A `telegram.send` that is not switched on is a message that
went into the simulator, and the run report will look entirely normal.

---

## The things that will not be obvious

- **The database is the deployment.** `runs/live.db` holds every customer, ticket,
  appointment, follow-up and conversation in flight. Back it up. Losing it loses more than
  losing the code.
- **A conversation is saved after every turn, not at the end.** There is no end — a
  conversation is abandoned far more often than it is finished, and the one that matters
  most to survive a restart is the one in the middle.
- **`world.store` is not for tools.** It is `None` in every scenario and every test, so a
  tool that reads it works right through development and raises on the first real customer.
  Everything durable is already reachable through the world's own methods.
- **The widget takes its API address from its own `<script src>`.** Serving it from staging
  and pointing it at production is not possible, which was the point.
- **Nothing about a world without a store has changed.** Two hundred and ninety tests depend
  on that, and none of them were edited to make the live path work.

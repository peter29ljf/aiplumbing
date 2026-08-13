Find out how many people, which day, and roughly what time they want to eat — then check
whether this party is yours to book or Kevin's.

Check the time with `clock.now` so you know what day it is. Ask for the party size, the
day, and a rough time. Then call `tables.check_party_size` with the number. It returns
`ok` or `needs_manager` — and you do not decide what "over eight" means, the tool does.

If it is `ok`, record the size and day on the ticket and go on to `offer_sittings`. If it
is `needs_manager`, do not offer sittings and do not promise anything — a table this size
is Kevin's to confirm, and that is the `large_party_handover` branch.

Write what they told you down before you move on. The size, the day, the rough time are
the whole job of this step, and a party that is not recorded is a party Kevin cannot
juggle.
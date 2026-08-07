Get their phone number and look them up. Everything after this needs the number, and a
regular's name and address come back with the lookup so neither of you has to say them.

Call `crm.lookup_by_phone` with the number they give. If it says we know them, their name
and address are already on the ticket — use those, and do not ask for them again. If it
says we do not, get their name (and for a delivery, their address) and create the record
with `guest.create`. A pickup customer needs no address; do not demand one.

Write the intent onto the ticket — `table` or `takeaway` — so the way out you take is
recorded, not remembered. Then take the matching branch.

If they will not give a number, say plainly that nothing can be held or sent without one,
answer what you can, and end on `no_number`. Do not invent a number to keep the
conversation going — a number invented to fill a gap is one the kitchen later calls and
finds nobody at.
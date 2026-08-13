# New client

A new client we have not worked with before. Collect the basics and open a record.

Get their name, an email, the language they prefer, and whether this is about a personal
or a corporate matter. Ask only for what is not already known — the phone is already on
the ticket from the lookup.

Then call `client.create` with what you have. It opens the record and remembers the name,
email, language and matter class, so you do not have to write those down by hand.

Write down with `ticket.set_fields` anything further they have told us about what they
came for, if it is not already captured.

End with `step.finished`. The next step hears what they need in their own words.
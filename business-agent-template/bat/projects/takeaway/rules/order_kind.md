Find out whether they want pickup or delivery, and whether the order is for now.

Check the time with `clock.now` and the hours with `rules.get_hours` first, so you know
whether the kitchen is even open. If they say the order is for another day, or the kitchen
has shut, take the `not_today` branch — do not start building an order that cannot be
made.

Otherwise ask plainly: pickup or delivery? Record the choice on the ticket with
`ticket.set_fields` — the whole of the order's shape hangs off it — and take the branch.
Pickup goes straight to taking the order. Delivery goes through `delivery_area` first,
because nothing about a delivery can be promised until the address is inside the 5 km.

Do not quote prices, waits, or the delivery fee here. Those come from the tools further
on, in that conversation, and quoting them now is quoting before you have looked them up.
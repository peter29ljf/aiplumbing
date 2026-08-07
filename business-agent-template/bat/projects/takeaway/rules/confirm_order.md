Read the order back with the total and the wait, and get a yes before anything is placed.

Call `order.quote` with the ticket — it returns the subtotal, the delivery fee (and
whether it is waived), the total, and the wait window, all in one call. State that total
and that wait to the customer exactly as the tool returned them; a total stated without
the fee, or a wait without the total, is a customer told half a thing. For a delivery,
say the fee is waived when the tool says it is, and say so in the same breath as the total
so it does not look like a surprise.

Then ask for a clear yes. If they say yes, record their confirmation and take the branch
matching the kind — pickup goes to `place_pickup`, delivery to `place_delivery`. If they
want to change something, go back and fix the order on the ticket before confirming again.

Do not say the order is placed. This step only confirms it; placing is the next step's
work, and saying "all set" here is a promise the placement step has not kept yet.
They want delivery. Take the address and check it is inside the 5 km before anything is
promised.

Ask for the delivery address. If they are a known customer the address may already be on
the ticket from the lookup — then you still check it, because the area is not a memory,
it is a measurement. Call `delivery.check_address` with the address, on its own, and read
what comes back. It tells you `in_area` or `out_of_area` and the fee and free-over
threshold — the 5 km is never your judgement.

If it is `in_area`, record the address and the check on the ticket and go on to take the
order. If it is `out_of_area`, take the `out_of_area` branch — do not promise a delivery
you cannot make, and do not round the distance down to fit.

Write the checked address down before you move on. An order placed without the checked
address is an order the kitchen cannot route.
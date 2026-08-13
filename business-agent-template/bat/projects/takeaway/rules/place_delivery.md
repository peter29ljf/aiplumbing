Place the delivery order, tell them the window and that it is paid at the door, tell
Kevin, and arrange to check it arrived.

Call `order.place` first, on its own, and read what comes back — the ready line and the
order reference. Say that line, because it is what they are agreeing to. If it refuses,
take the answer and do not claim the order is placed. If the kitchen must confirm an
allergen question first, pass that on.

Then, with that answer in front of you, the rest together: tell Kevin with `manager.notify`
so the kitchen cooks it and knows where it is going, text the customer with `sms.send` so
they have the window, and arrange the check — `schedule.create_followup` so Kevin is asked
whether the delivery arrived. That check is what makes this a delivery and not a pickup;
a delivery nobody checks on is a delivery that may have gone to the wrong door.

Then tell the customer it is paid at the door, not in chat. One message, right the first
time.
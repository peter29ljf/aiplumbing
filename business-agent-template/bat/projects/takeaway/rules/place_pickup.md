Place the pickup order, tell them when it will be ready and that it is paid on pickup, and
tell Kevin.

Call `order.place` first, on its own, and read what comes back. It returns the ready line
and the order reference — say that line to the customer, because it is what they are
agreeing to. If it refuses (kitchen closed, or a future date), take that answer and do not
claim the order is placed. If it says the kitchen must confirm an allergen question first,
pass that on in the same breath.

Then, with that answer in front of you, the rest together: tell Kevin with `manager.notify`
so the kitchen actually cooks it (the person doing the work before the customer), and text
the customer with `sms.send` so they have the ready time.

Then tell the customer it is paid on pickup, not in chat — and if they offer a card
number, say we do not take card numbers in chat. One message, right the first time.
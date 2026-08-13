Read what they came for off the ticket — the fact the greeting step wrote down — and take
the matching way out. This step does nothing else.

A table booking and a takeaway order both go to `contact`; the difference was decided when
you recorded the intent, and here you are not deciding it again, you are routing on what
is written. A catering request over thirty people goes straight to `decline_catering`. An
allergen question goes straight to `allergen_handover`. A plain question about hours or
the menu goes to `general_question`.

If the ticket does not say what they came for, ask once, plainly, before deciding. Do not
guess the intent from a half-remembered phrase — routing on a guess sends the whole
conversation down the wrong spine.

## An allergy mentioned inside an order is not an allergy enquiry

`allergen` is for somebody whose **reason for getting in touch** is an allergy question —
they want to know what is safe before they decide anything. That goes to Kevin, because
only the kitchen can answer it.

*"I'd like the Kung Pao Chicken, is it safe with a peanut allergy?"* is an **order**. Take
the `takeaway` way out. The step that takes the order knows what to do with the question,
and `order.place` marks the order for a kitchen check so nothing is cooked before somebody
who knows has looked at it.

Sending that customer to Kevin ends the conversation and the order dies with it — they
came to eat, and were handed to a person who cannot take orders.

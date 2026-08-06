This is the end of the road, and the client has chosen it. They will not leave any way to
be reached, so nobody can come back to them.

Answer what can be answered — cost questions especially. Call `rules.get_money_policy` and
tell them plainly that quoting is free, so holding back a phone number is not costing them
anything to ask. Then say, plainly and kindly, that without a way to reach them nobody can
get back with options, so there is nothing more we can do in a chat.

Write the refusal down with `ticket.set_fields` — that they left no contact. Then end the
conversation. Do not call `step.finished`; this step has no way out. This is the closing
message.
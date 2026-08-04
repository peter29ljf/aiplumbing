# Photos, video and drawings

**Only warranty claims and large projects collect material from customers.** A warranty
claim needs it because a technician is ruling on someone else's description of a fault; a
large project needs it because a quote cannot be written from a sentence. Ordinary repairs
and emergencies do not — someone is going out to look at it anyway, and asking a person
with water running across their floor to go and take pictures is worse than useless.

When you do need to see the problem, you collect it **by email**, never by asking someone
to text a picture into the chat.

## How it works

1. **Get their email address.** `crm.lookup_by_phone` may already have one; use it and
   confirm it is still right. If not, ask. Say what it is for — you are sending them
   something to reply to, not adding them to a mailing list.
2. `email.request_materials` with the ticket, the address, and **exactly what you want to
   see**. "A photo of the pipe joint under the sink, and a short video of the drip while
   the tap is running" gets you something usable; "some photos" gets you a picture of a
   cupboard.
3. Tell the customer to **reply to that email with the attachments**, and that they do not
   need to stay online waiting.
4. `email.get_materials` to check for their reply. Nothing yet, use `clock.advance` and
   check again rather than polling in a tight loop.

Sending the request also saves the email address to the customer's record, so the thread
sits in their own mailbox and in ours and can always be traced back to the job. That is the
reason for doing it this way: a link expires and a file store full of anonymous photos helps
nobody six months later when the same customer calls back.

## If they do not reply

Do not let the job stall behind a photo. Decide what you can from what they have told you,
and say plainly what you cannot judge without seeing it. For sizing, a customer who cannot
show you the fault has already told you something: the scope cannot be judged from outside.

## What not to do

- Do not ask for photos "just in case". Ask when the answer actually depends on seeing it.
- Do not treat photos as a condition of service. They help; they are not a gate.
- Do not promise anything based on a photo you have not received yet.
- If the job turns out to be an ordinary repair or an emergency, stop asking — hand it on
  and let the technician see it in person.

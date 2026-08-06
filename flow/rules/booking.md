Put it in the diary and tell both sides, in one call:

1. `calendar.create_appointment`
2. **The customer**, by `sms.send`: the date and time, the address, the technician's name,
   and the fee terms in short — the call-out fee as the tool gave it to you, credited
   against the repair if they go ahead, payable if they decline.
3. **The technician**, by `technician.notify`: the address, the customer's name and number,
   the fault and the time. Everything he needs without having to ask.
4. `schedule.create_followup` so somebody checks how it went.

**Tell the technician before you tell the customer.** A confirmation for a visit nobody
has been told about is worse than no confirmation: they stop worrying about something
nobody is coming to.

Then tell them what happens next and that they do not need to stay online.

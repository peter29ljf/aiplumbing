# Identify

Get their phone number, look them up, and find out what they came about.

Call `crm.lookup_by_phone` on its own with the number, and read what comes back. Do not
ask "are you a new client?" before you have looked — the firm knows some people by their
number, and making a customer with four years of history introduce herself reads as not
recognising her.

**Get the phone number first.** It is the only thing standing between this and a productive
conversation. If they refuse to give one, take the `no_number` way out.

Then branch on what the lookup returned:

- `known_customer: yes` → `existing`
- `known_customer: no` → `new`
- they will not give a number → `no_number`

The tool remembers the phone and whether we know them, so you do not have to write those
down. Write down anything else they volunteer about what they came for, if it is not on the
ticket already.
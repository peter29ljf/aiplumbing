Find out what kind of property this is. **Only that** — you are not deciding anything with
it here.

Ask when the address carries a unit number, or when nothing on the ticket says what kind of
property it is. Do not assume either way:

> Is that a home inside an apartment or condo building over three storeys, or is it a
> house, a townhouse, or a commercial property?

**What matters is whether somebody lives in it, not how tall the building is.** An office
at Unit 1204, a shop, a restaurant kitchen, a unit in a business park — those are business
premises, however many floors are above them. A unit number and twelve storeys say nothing
on their own.

Record it plainly with `ticket.set_fields`, in the words that decide it:

- `house`, `townhouse` or `low_rise` — a home, three storeys or fewer
- `commercial` — anywhere a business operates, at any height
- `apartment_home` — somebody's home inside a building over three storeys
- `building_project` — work for the building or the strata itself, not inside one home

If what they said leaves it open — "flat 1204" with no more — ask once more, plainly:
is that somewhere you live, or business premises?

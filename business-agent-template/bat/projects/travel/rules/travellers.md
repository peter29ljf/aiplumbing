Who is going. Ask how many adults, how many children, and each child's age. Ages matter —
they decide pricing for the whole party, so a child without an age is a child who might
be priced as an adult or a lap.

Adults: a count is enough. Children: a count is not enough — you need each child's age,
and you need to press until you have them all. A client who says "two kids" and nothing
else is a client who has not answered yet; ask again, kindly, until each child has an age.

Write it all with `ticket.set_fields`: number of adults, number of children, and a list of
the children's ages. If a child came with an exact age, that is what goes down — a child's
age as an age, never a guessed year of birth.

Then `step.finished`. This is the only fact this step owns, so get it right: the party is
the one thing the consultant cannot reconstruct from anywhere else.
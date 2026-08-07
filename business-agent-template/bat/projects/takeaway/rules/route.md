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
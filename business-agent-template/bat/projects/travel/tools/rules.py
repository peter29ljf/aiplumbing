"""Rules for Pacific Compass Travel — the agency's exact wording and routing, all in
code so the model never paraphrases into a promise somebody else has to keep."""

from __future__ import annotations

from bat.runtime.registry import Refused, tool
from bat.runtime.world import AnyWorld


@tool(
    "rules.get_money_policy",
    "One call answering every cost question a client asks: quoting is free, the deposit "
    "is 20% and the consultant takes it (never the chat), and insurance is always quoted "
    "separately. Returns the three sentences, verbatim, so the reply can use them.",
    {},
)
def rules_get_money_policy(world: AnyWorld) -> dict:
    return {
        "quoting_free": (
            "Quoting is free — no fee, no card, nothing is taken in the chat."
        ),
        "deposit": (
            "A 20% deposit is taken by your consultant when you book — never anything "
            "in this chat."
        ),
        "insurance": (
            "Travel insurance is always quoted separately, alongside the trip."
        ),
    }


@tool(
    "rules.get_decline",
    "Returns the agency's exact refusal sentence for one of three keys: 'destination' "
    "(a place we do not book), 'visa' (we do not handle visa applications), or "
    "'insurance_only' (we only sell insurance alongside a trip we booked). Pass the key "
    "and read the returned 'line' back to the client verbatim.",
    {"key": {"type": "string", "description": "'destination', 'visa', or 'insurance_only'"}},
)
def rules_get_decline(world: AnyWorld, key: str) -> dict:
    lines = {
        "destination": (
            "We're not able to arrange travel to Cuba, I'm afraid — it's not somewhere "
            "we book. If there's anywhere else you're considering, I'd be glad to help."
        ),
        "visa": (
            "We don't handle visa applications, so that part you'd do yourself. Your "
            "consultant will tell you which visas you need when they come back with "
            "the options."
        ),
        "insurance_only": (
            "We only arrange travel insurance alongside a trip we've booked for you, "
            "so on its own it isn't something we can sell. If you're planning a trip, "
            "we'd be glad to quote both together."
        ),
    }
    if key not in lines:
        raise Refused(
            f"I do not have a refusal line for '{key}'. Use 'destination', 'visa', "
            "or 'insurance_only'."
        )
    return {"key": key, "line": lines[key]}


@tool(
    "rules.check_destination",
    "Decides, in code, whether Pacific Compass will arrange travel to the destination, "
    "which region it lies in, and whose enquiry it is. Refuses Cuba and Iran outright. "
    "Returns ok, region, and the consultant who covers it. The region and consultant "
    "are written to the ticket for you.",
    {"destination": {"type": "string", "description": "The country or place they want to go"}},
    remembers=("region", "consultant"),
)
def rules_check_destination(world: AnyWorld, destination: str) -> dict:
    d = (destination or "").strip().lower()
    if d in ("cuba", "iran"):
        return {"ok": False, "refused": True, "destination": destination}

    region, consultant = _consultant_for(destination)
    return {"ok": True, "refused": False, "region": region, "consultant": consultant}


def _consultant_for(destination: str):
    """Route by destination. Sam covers Asia; Priya covers Europe and the Americas.
    Anything else — and any multi-region trip — goes to whoever is on the rota."""
    d = (destination or "").lower()
    asia = ["japan", "tokyo", "thailand", "bangkok", "vietnam", "china", "india",
            "korea", "seoul", "singapore", "malaysia", "bali", "indonesia",
            "philippines", "taiwan", "hong kong", "cambodia", "laos", "myanmar",
            "nepal", "sri lanka", "mongolia", "kazakhstan", "uzbekistan", "siberia"]
    if any(k in d for k in asia):
        return "Asia", "Sam"

    europe = ["france", "paris", "italy", "rome", "lisbon", "portugal", "spain",
              "madrid", "germany", "berlin", "uk", "london", "england", "ireland",
              "dublin", "greece", "athens", "netherlands", "amsterdam", "switzerland",
              "zurich", "austria", "vienna", "belgium", "brussels", "croatia", "czech",
              "prague", "denmark", "sweden", "norway", "finland", "poland", "hungary",
              "budapest", "scotland", "wales"]
    americas = ["usa", "united states", "new york", "california", "canada", "toronto",
                "vancouver", "mexico", "cancun", "brazil", "rio", "argentina",
                "buenos aires", "chile", "peru", "lima", "colombia", "bogota",
                "costa rica", "panama", "ecuador", "guatemala", "caribbean", "jamaica",
                "cuba", "dominican", "hawaii"]
    if any(k in d for k in europe):
        return "Europe", "Priya"
    if any(k in d for k in americas):
        return "Americas", "Priya"

    return "Other", "the enquiry rota"
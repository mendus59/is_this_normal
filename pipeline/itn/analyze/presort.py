"""Free pre-sort of routine executive documents. These follow fixed templates or are
required by statute on a fixed schedule: ceremonial proclamations, continuations of an
existing emergency, routine delegations and successions, annual statutory determinations,
pay adjustments, holiday closings, and trade-agreement implementation. They are marked
Routine with no model call and labeled "routine template". Anything the rules are unsure
about goes to the model."""

import re

_CEREMONIAL = re.compile(
    r"\b(National|World|International)\b.*\b(Day|Week|Month|Year|Anniversary)\b"
    r"|\b(Day|Week|Month)\b.*,\s*(19|20)\d\d$"
    r"|^(Loyalty|Law|Father's|Mother's|Columbus|Thanksgiving|Memorial|Veterans|Flag|Labor)\b"
    r"|Day of (Prayer|Remembrance|Mourning)"
    r"|^Honoring the (Memory|Victims)"
    r"|Death of .*(former|President|Senator|Justice)"
    r"|Half[- ]Staff"
    , re.IGNORECASE)
# Words that mean a proclamation does something even if it names a day or week.
_SUBSTANTIVE = re.compile(r"\b(tariff|import|duty|quota|suspension|entry|emergency|sanction|adjust|boundary|monument|modif)",
                          re.IGNORECASE)

# (reason, pattern) checked in order against the title.
_RULES = [
    ("continuation of an existing emergency",
     re.compile(r"^Continuation of (the )?(National )?Emergency|^Continuation of the Effect", re.IGNORECASE)),
    ("routine delegation, designation or order of succession",
     re.compile(r"^(Delegation of (Authority|Functions|Certain)|Designation of (Officers?|an? Officer)"
                r"|(Providing an )?Order of Succession|Assignment of (Functions|Certain))", re.IGNORECASE)),
    ("annual statutory determination",
     re.compile(r"Refugee Admissions for Fiscal Year|Trafficking in Persons|Major (Illicit )?Drug (Transit|Producing)"
                r"|Jerusalem Embassy Act|Unexpected Urgent Refugee and Migration Needs"
                r"|Emergency Refugee and Migration Assistance", re.IGNORECASE)),
    ("annual pay adjustment",
     re.compile(r"^Adjustments? of Certain Rates of Pay", re.IGNORECASE)),
    ("holiday closing of federal offices",
     re.compile(r"^(Providing for the )?Closing of (Executive|Federal) (Departments|Government)", re.IGNORECASE)),
    ("routine advisory-committee continuance",
     re.compile(r"^Continuance of Certain Federal Advisory Committees", re.IGNORECASE)),
    ("railway labor emergency board",
     re.compile(r"^Establishing an Emergency Board", re.IGNORECASE)),
    ("trade-agreement or preference-program implementation",
     re.compile(r"^To (Implement|Modify|Take Certain Actions Under) (the )?"
                r"(.*Trade (Promotion )?Agreement|Duty-Free Treatment|.*Generalized System of Preferences"
                r"|.*African Growth and Opportunity Act|.*Harmonized Tariff Schedule)", re.IGNORECASE)),
]


def routine_reason(subtype: str | None, title: str) -> str | None:
    """Why a document is a routine template, or None when the model should score it."""
    t = (title or "").strip()
    for reason, pattern in _RULES:
        if pattern.search(t):
            return reason
    if subtype in (None, "proclamation") and _CEREMONIAL.search(t) and not _SUBSTANTIVE.search(t):
        return "ceremonial proclamation"
    return None

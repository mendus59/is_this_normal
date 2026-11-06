"""Free pre-sort of routine executive documents: ceremonial proclamations, annual
continuations of an existing emergency, and routine delegations follow fixed templates.
They are marked Routine with no model call and labeled "routine template". Anything
the rules are unsure about goes to the model."""

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
_CONTINUATION = re.compile(r"^Continuation of (the )?National Emergency", re.IGNORECASE)
_DELEGATION = re.compile(r"^(Delegation of (Authority|Functions)|Designation of Officers?|Order of Succession|Providing an Order of Succession)",
                         re.IGNORECASE)
# Words that mean a proclamation does something even if it names a day or week.
_SUBSTANTIVE = re.compile(r"\b(tariff|import|duty|quota|suspension|entry|emergency|sanction|adjust|boundary|monument|modif)",
                          re.IGNORECASE)


def routine_reason(subtype: str | None, title: str) -> str | None:
    """Why a document is a routine template, or None when the model should score it."""
    t = (title or "").strip()
    if _CONTINUATION.search(t):
        return "annual continuation of an existing national emergency"
    if _DELEGATION.search(t):
        return "routine delegation or order of succession"
    if subtype in (None, "proclamation") and _CEREMONIAL.search(t) and not _SUBSTANTIVE.search(t):
        return "ceremonial proclamation"
    return None

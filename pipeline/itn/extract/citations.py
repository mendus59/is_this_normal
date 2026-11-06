"""Find citations to Supreme Court cases, the U.S. Code, and public laws in opinion text.

Uses eyecite (Free Law Project). Only full citations are kept: short forms like
"Id." and "supra" point back at a full citation that is already counted.
"""

import re

from eyecite import get_citations
from eyecite.models import FullCaseCitation, FullLawCitation

from itn import ids

EXTRACTOR = "eyecite-v1"
CONTEXT_CHARS = 200
_SECTION = re.compile(r"^[0-9A-Za-z.\-]+$")


def _target(cite) -> str | None:
    # eyecite leaves a group as None for slip cites like "597 U. S. ___".
    g = {k: (v or "") for k, v in cite.groups.items()}
    if isinstance(cite, FullCaseCitation):
        if cite.corrected_reporter() == "U.S." and g.get("volume", "").isdigit() \
                and g.get("page", "").isdigit():
            return ids.scotus_case(g["volume"], g["page"])
    elif isinstance(cite, FullLawCitation):
        reporter = g.get("reporter", "")
        if reporter == "U.S.C." and g.get("title", "").isdigit() and _SECTION.match(g.get("section", "")):
            return ids.usc_section(g["title"], g["section"])
        if reporter == "Pub. L." and re.fullmatch(r"\d+-\d+", g.get("title", "")):
            return ids.public_law(*g["title"].split("-"))
    return None


def extract(text: str, citing_date: str | None = None) -> list[dict]:
    """Citation rows (without from_component/extractor) for one opinion's text."""
    rows, seen = [], set()
    if not (text or "").strip():
        return rows
    for cite in get_citations(text):
        target = _target(cite)
        if not target:
            continue
        pin = getattr(cite.metadata, "pin_cite", None)
        raw = cite.matched_text()
        if (target, pin, raw) in seen:
            continue
        seen.add((target, pin, raw))
        start, end = cite.span()
        rows.append({
            "to_artifact": target,
            "pinpoint": pin,
            "raw_text": raw,
            "context_snippet": text[max(0, start - CONTEXT_CHARS):end + CONTEXT_CHARS].strip(),
            "citing_date": citing_date,
        })
    return rows


# Federal Register documents and enrolled laws cite the Code as "8 U.S.C. 1182(f)",
# without the section sign eyecite expects.
_BARE_USC = re.compile(r"\b(\d{1,2})\s+U\.\s?S\.\s?C\.\s+(?:§+\s*)?(\d+[a-z]?(?:-\d+[a-z]?)?)")


def extract_statutes(text: str) -> list[str]:
    """U.S. Code section ids cited in text, most cited first, including bare forms."""
    counts: dict[str, int] = {}
    for m in _BARE_USC.finditer(text or ""):
        aid = ids.usc_section(m.group(1), m.group(2))
        counts[aid] = counts.get(aid, 0) + 1
    return sorted(counts, key=lambda a: (-counts[a], a))

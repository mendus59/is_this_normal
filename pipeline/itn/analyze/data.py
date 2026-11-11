"""Data tools for the paid stages. Every query that feeds a model filters to material
dated on or before the item's as-of date (the day before its effective date). This is
enforced here, in the data layer, not only in prompts."""

import datetime as dt
import re
from dataclasses import dataclass, field

from itn.analyze.actors import actor
from itn.analyze.estimate import CONTROLLING
from itn.analyze.presort import routine_reason
from itn.extract.citations import extract_statutes

TEXT_MAX_CHARS = 120_000        # per item; the summary stage reads at most this much
AUTHORITY_TEXT_MAX_CHARS = 60_000
CONTEXT_CHARS = 300
_WORD = re.compile(r"[A-Za-z]{4,}")
_STOP = {"with", "that", "this", "from", "into", "under", "united", "states", "america", "certain",
         "other", "their", "which", "section", "purposes", "provide", "act", "relating"}


def as_of(effective_date) -> dt.date:
    d = effective_date if isinstance(effective_date, dt.date) else dt.date.fromisoformat(str(effective_date)[:10])
    return d - dt.timedelta(days=1)


@dataclass
class Item:
    id: str
    type: str
    subtype: str | None
    title: str
    effective_date: dt.date
    summary: str | None
    metadata: dict
    text_keys: list[str] = field(default_factory=list)

    @property
    def as_of(self) -> dt.date:
        return as_of(self.effective_date)

    @property
    def actor(self) -> str | None:
        return actor(self.type, self.id, self.effective_date, self.metadata)

    @property
    def presort(self) -> str | None:
        return routine_reason(self.subtype, self.title) if self.type == "executive_document" else None


def _text_keys(conn, aid: str) -> list[str]:
    comps = conn.execute("SELECT kind, text_key FROM component WHERE artifact_id = %s AND text_key IS NOT NULL"
                         " ORDER BY ord", (aid,)).fetchall()
    lead = [k for kind, k in comps if kind in CONTROLLING] or [k for kind, k in comps if kind != "dissent"]
    if lead:
        return lead
    row = conn.execute("SELECT text_key FROM artifact WHERE id = %s", (aid,)).fetchone()
    return [row[0]] if row and row[0] else []


def load_items(conn, ids: list[str]) -> list[Item]:
    rows = conn.execute("SELECT id, type, subtype, title, effective_date, official_summary, metadata"
                        " FROM artifact WHERE id = ANY(%s) AND effective_date IS NOT NULL", (ids,)).fetchall()
    items = [Item(r[0], r[1], r[2], r[3], r[4], r[5], r[6] or {}) for r in rows]
    for it in items:
        it.text_keys = _text_keys(conn, it.id)
    conn.commit()
    return sorted(items, key=lambda i: (i.effective_date, i.id))


def text_of(r2, keys: list[str], max_chars: int) -> str:
    out, n = [], 0
    for k in keys:
        t = r2.get_text(k) or ""
        out.append(t[: max_chars - n])
        n += len(out[-1])
        if n >= max_chars:
            break
    return "\n\n".join(out)


# Cut long summaries in the database, not after the fact: the free Neon plan meters
# network transfer, and full CRS summaries of every cited law used most of it.
CITED_SUMMARY_CHARS = 4000


def citations(conn, r2, item: Item, text: str | None = None) -> list[dict]:
    """What the item cites, most cited first, each with one context snippet. Only cited
    artifacts dated on or before the as-of date (or undated, like Code sections) are kept."""
    rows = conn.execute(
        "SELECT min(c.id), c.to_artifact, min(c.context_snippet), count(*) FROM citation c"
        " JOIN component f ON f.id = c.from_component"
        " WHERE f.artifact_id = %s AND f.kind = ANY(%s)"
        " GROUP BY c.to_artifact ORDER BY count(*) DESC, c.to_artifact",
        (item.id, list(CONTROLLING))).fetchall()
    seen, out = set(), []
    for cid, to, ctx, _ in rows:
        if to not in seen and to != item.id:
            seen.add(to)
            out.append({"citation_id": cid, "to": to, "context": ctx or ""})
    if not out:  # executive documents and laws: statutes cited in the text itself
        text = text if text is not None else text_of(r2, item.text_keys, TEXT_MAX_CHARS)
        for to in extract_statutes(text):
            m = re.search(r"\b" + re.escape(to.split("/")[1]) + r"\s+U\.\s?S\.\s?C\.\s+(?:§+\s*)?"
                          + re.escape(to.split("/")[2]), text, re.IGNORECASE)
            ctx = text[max(0, m.start() - CONTEXT_CHARS): m.end() + CONTEXT_CHARS] if m else ""
            out.append({"citation_id": None, "to": to, "context": ctx})
    if not out:
        return []
    allowed = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, title, effective_date, left(official_summary, %s), type FROM artifact WHERE id = ANY(%s)"
        " AND (effective_date IS NULL OR effective_date <= %s)",
        (CITED_SUMMARY_CHARS, [c["to"] for c in out], item.as_of)).fetchall()}
    conn.commit()
    kept = []
    for c in out:
        if c["to"] in allowed:
            title, date, summary, typ = allowed[c["to"]]
            kept.append({**c, "title": title, "date": date.isoformat() if date else None,
                         "summary": summary or "", "type": typ})
    return kept


def analog_candidates(conn, item: Item, limit: int = 12) -> list[dict]:
    """Earlier items of the same type that look most alike, with their actor, so the model
    can weigh the same actor's own history less. Half come from title matches alone (so a
    long summary cannot crowd out laws of the same kind, like earlier reconciliation acts),
    the rest from title and summary."""
    words = []
    for w in _WORD.findall(f"{item.title} {item.summary or ''}"[:4000].lower()):
        if w not in _STOP and w not in words:
            words.append(w)
    title_words = [w for w in words if w in item.title.lower()]
    found: dict[str, tuple] = {}
    for fields, ws, n in (("title", title_words, limit // 2),
                          ("title || ' ' || coalesce(official_summary, '')", words, limit)):
        if not ws:
            continue
        query = " | ".join(ws[:24])
        rows = conn.execute(
            "SELECT id, type, title, effective_date, left(official_summary, 1500), metadata FROM artifact"
            f" WHERE type = %s AND in_scope AND id <> %s AND effective_date <= %s"
            f" AND to_tsvector('english', {fields}) @@ to_tsquery('english', %s)"
            f" ORDER BY ts_rank(to_tsvector('english', {fields}), to_tsquery('english', %s)) DESC,"
            " effective_date DESC LIMIT %s",
            (item.type, item.id, item.as_of, query, query, n)).fetchall()
        for r in rows:
            if len(found) < limit:
                found.setdefault(r[0], r)
    conn.commit()
    me = item.actor
    out = []
    for aid, typ, title, date, summary, meta in found.values():
        who = actor(typ, aid, date, meta or {})
        out.append({"id": aid, "title": title, "date": date.isoformat(), "actor": who,
                    "same_actor": bool(me and who == me), "summary": (summary or "")[:1500]})
    return out


VEHICLES = {"reconciliation": "budget reconciliation", "continuing appropriations": "a continuing resolution",
            "consolidated appropriations": "an omnibus appropriations act"}


_OVERRULE = re.compile(r"[^.]{0,200}\b(?:overrul(?:e|ed|es|ing)|abrogat(?:e|ed|es))\b[^.]{0,200}\.?", re.IGNORECASE)


# An express, affirmative overruling ("Roe and Casey are overruled", "we overrule"), not
# "we decline to overrule" or a plurality that "would overrule".
_EXPRESS = re.compile(
    r"\b(?:is|are|were|was|stands?|must\s+be)\s+(?:hereby\s+|now\s+|accordingly\s+|therefore\s+|thus\s+)?overruled\b"
    r"|\bwe\s+(?:now\s+|therefore\s+|today\s+|hereby\s+)?overrule\b", re.IGNORECASE)
# Also not a question or an argument about overruling ("whether Saucier should be overruled",
# "petitioner argues Austin was wrongly decided and is overruled by...").
_NEGATED = re.compile(r"\b(?:not|decline[sd]?|need not|should not|would not|cannot|no reason to|unnecessary to"
                      r"|whether|would|argu(?:e[sd]?|ing)|urg(?:e[sd]?|ing)|contend(?:s|ed)?|ask(?:s|ed)?)\b"
                      r"[^.]{0,60}$", re.IGNORECASE)

# Statutes that give the President emergency or extraordinary powers. Invoking one is a
# fact the procedure dimension weighs; it is not by itself a departure.
EMERGENCY_STATUTES = {
    "usc/50/1621": "National Emergencies Act", "usc/50/1622": "National Emergencies Act",
    "usc/50/1631": "National Emergencies Act", "usc/50/1701": "IEEPA", "usc/50/1702": "IEEPA",
    "usc/50/1703": "IEEPA", "usc/50/1704": "IEEPA", "usc/50/1705": "IEEPA",
    "usc/8/1182": "INA 212(f) entry suspension", "usc/19/1862": "Section 232 (national security tariffs)",
    "usc/19/2411": "Section 301 (trade retaliation)", "usc/19/2253": "Section 201 (safeguard tariffs)",
    "usc/10/2808": "Military construction in a national emergency", "usc/10/12302": "Ready Reserve call-up",
    "usc/50/4511": "Defense Production Act", "usc/42/5191": "Stafford Act emergency",
}
STATUTE_HISTORY_MAX = 8


def express_overrulings(text: str) -> list[str]:
    """Sentences in which the opinion itself overrules, free of negation."""
    out = []
    for m in _OVERRULE.finditer(text or ""):
        sentence = " ".join(m.group(0).split())
        e = _EXPRESS.search(sentence)
        if e and not _NEGATED.search(sentence[:e.start()]):
            out.append(sentence)
    return list(dict.fromkeys(out))


def statute_history(conn, item: Item, cited: list[str]) -> list[dict]:
    """For each U.S. Code section the document cites: how many earlier in-scope documents
    of the same type cited it, by how many different actors, and since when. Counts only
    material dated on or before the as-of date."""
    out = []
    for sec in [c for c in cited if c.startswith("usc/")][:STATUTE_HISTORY_MAX]:
        rows = conn.execute(
            "SELECT DISTINCT a.id, a.effective_date, a.metadata FROM citation c"
            " JOIN component f ON f.id = c.from_component JOIN artifact a ON a.id = f.artifact_id"
            " WHERE c.to_artifact = %s AND a.type = %s AND a.in_scope AND a.id <> %s"
            " AND a.effective_date <= %s ORDER BY a.effective_date", (sec, item.type, item.id, item.as_of)).fetchall()
        actors = [actor(item.type, aid, d, m or {}) for aid, d, m in rows]
        others = {a for a in actors if a and a != item.actor}
        out.append({"section": sec, "earlier_items": len(rows), "earlier_by_other_actors": sum(1 for a in actors if a != item.actor),
                    "other_actors": sorted(others), "first_use": rows[0][1].isoformat() if rows else None,
                    "examples": [r[0] for r in rows[-3:]]})
    conn.commit()
    return out


RARE_USE = 3
COMMON_NOTE = ("These provisions were cited often before. That shows the power exists, not that earlier"
               " uses had this reach: judge scope and novelty from what this item itself does.")


def statute_facts(history: list[dict]) -> dict:
    """Split statute history so only rarity carries weight. A rarely used provision is a
    real signal; a commonly cited one says little about whether this use is ordinary, and
    raw counts led the model to rate sweeping uses of familiar powers as ordinary."""
    rare = [h for h in history if h["earlier_by_other_actors"] <= RARE_USE]
    common = [h["section"] for h in history if h["earlier_by_other_actors"] > RARE_USE]
    out: dict = {"rarely_used_provisions": rare or "none"}
    if common:
        out["commonly_cited_provisions"] = {"sections": common, "note": COMMON_NOTE}
    return out


def record_facts(conn, item: Item, text: str | None = None, cited: list[str] | None = None) -> dict:
    """Free facts from the record that a short excerpt may not show. Rulings: passages of
    the controlling opinion that speak of overruling, and whether it expressly overrules.
    Executive documents: emergency powers invoked and each cited statute's history of use.
    Laws: final-passage roll calls, how often the same vehicle was used before, size."""
    cited = cited or []
    if item.type == "scotus_case":
        if text is None:
            return {}
        hits = [" ".join(m.group(0).split()) for m in _OVERRULE.finditer(text)]
        hits = list(dict.fromkeys(hits))[:6]
        express = express_overrulings(text)[:3]
        return {"overruling_passages": hits or "none in the controlling opinion",
                "expressly_overrules": express or False}
    if item.type == "executive_document":
        powers = sorted({EMERGENCY_STATUTES[c] for c in cited if c in EMERGENCY_STATUTES})
        return {"emergency_powers_invoked": powers or "none of the listed emergency statutes",
                **statute_facts(statute_history(conn, item, cited))}
    if item.type != "public_law":
        return {}
    facts: dict = {}
    usc = [c for c in cited if c.startswith("usc/")]
    if usc:
        facts["us_code_cited"] = {"sections": len(usc), "titles": len({c.split("/")[1] for c in usc})}
    votes = [v for v in (item.metadata.get("votes") or []) if (v.get("date") or "") <= item.as_of.isoformat()]
    if votes:
        facts["final_passage_votes"] = [
            {"chamber": v.get("chamber"), "date": v.get("date"), "yeas": v.get("yeas"), "nays": v.get("nays")}
            for v in votes]
    else:
        facts["final_passage_votes"] = "none recorded (passed by voice vote or unanimous consent)"
    # How large it is next to earlier substantive laws, by the length of the CRS summary.
    size = len(item.summary or "")
    if size:
        row = conn.execute(
            "SELECT count(*) FILTER (WHERE length(official_summary) < %s), count(*) FROM artifact"
            " WHERE type = 'public_law' AND in_scope AND id <> %s AND effective_date <= %s"
            " AND official_summary IS NOT NULL", (size, item.id, item.as_of)).fetchone()
        conn.commit()
        if row and row[1]:
            facts["size_percentile"] = (f"{round(100 * row[0] / row[1])} (its official summary is longer than"
                                        f" that of {row[0]} of {row[1]} earlier substantive laws)")
    low = item.title.lower()
    for key, name in VEHICLES.items():
        if key in low:
            rows = conn.execute(
                "SELECT id, title FROM artifact WHERE type = 'public_law' AND id <> %s"
                " AND effective_date <= %s AND title ILIKE %s ORDER BY effective_date",
                (item.id, item.as_of, f"%{key}%")).fetchall()
            conn.commit()
            facts["vehicle"] = (f"Enacted through {name}. Earlier laws since 2000 enacted the same way:"
                                f" {len(rows)}" + (f" (e.g. {', '.join(r[0] for r in rows[-6:])})" if rows else ""))
            break
    return facts


def same_actor_finals(conn, item: Item, ids: list[str]) -> dict[str, dict]:
    """Current final dimension ratings of the actor's own earlier, already scored items."""
    if not ids or not item.actor:
        return {}
    rows = conn.execute(
        "SELECT a.id, a.type, a.effective_date, a.metadata, s.dimensions FROM artifact a"
        " JOIN score s ON s.artifact_id = a.id AND s.current"
        " WHERE a.id = ANY(%s) AND a.effective_date <= %s", (ids, item.as_of)).fetchall()
    conn.commit()
    return {aid: dims for aid, typ, date, meta, dims in rows
            if actor(typ, aid, date, meta or {}) == item.actor}

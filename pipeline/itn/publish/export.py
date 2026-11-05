"""Export the database to static files in R2 for the front end.

Each publish writes under a new version prefix, then replaces manifest.json, so a
reader never sees a half-written update. Rolling back means pointing the manifest
at an older prefix.
"""

import datetime as dt
import json
from bisect import bisect_right

from itn.storage import R2

BANDS = [(80, "routine"), (60, "ordinary"), (40, "unusual"), (20, "novel"), (0, "unprecedented")]


def band(percentile: float) -> str:
    return next(name for floor, name in BANDS if percentile >= floor)


def percentiles(raw_scores: dict[str, float]) -> dict[str, float]:
    """Percentile of each raw score among all scores (0-100, higher = more normal)."""
    ordered = sorted(raw_scores.values())
    n = len(ordered)
    if n == 0:
        return {}
    if n == 1:
        return {k: 50.0 for k in raw_scores}
    return {k: round(100 * (bisect_right(ordered, v) - 1) / (n - 1), 1) for k, v in raw_scores.items()}


def build_catalog(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.type, a.subtype, a.title, a.effective_date, a.in_scope, a.contentious,
               a.official_summary, a.sources, s.raw, s.confidence
        FROM artifact a
        LEFT JOIN score s ON s.artifact_id = a.id AND s.current
        WHERE a.in_scope
        ORDER BY a.effective_date DESC NULLS LAST, a.id
        """
    ).fetchall()
    raw = {r[0]: r[9] for r in rows if r[9] is not None}
    pct = percentiles(raw)
    catalog = []
    for (aid, typ, subtype, title, date, in_scope, contentious, summary, sources, _raw, conf) in rows:
        entry = {
            "id": aid, "type": typ, "subtype": subtype, "title": title,
            "date": date.isoformat() if date else None,
            "summary": summary, "sources": sources,
        }
        if aid in pct:
            entry.update(percentile=pct[aid], band=band(pct[aid]), confidence=conf)
        catalog.append(entry)
    return catalog


MAX_CITED_BY = 300


def build_details(conn) -> dict[str, dict]:
    """One reader file per artifact that is in scope or cited by one: its opinions (with
    the R2 key of each one's text), what it cites, and what cites it."""
    arts = {}
    for (aid, typ, subtype, title, date, in_scope, summary, sources, text_key) in conn.execute(
            "SELECT id, type, subtype, title, effective_date, in_scope, official_summary, sources,"
            " text_key FROM artifact WHERE in_scope OR closure_depth = 1"):
        arts[aid] = {"id": aid, "type": typ, "subtype": subtype, "title": title,
                     "date": date.isoformat() if date else None, "in_scope": in_scope,
                     "summary": summary, "sources": sources, "text": text_key,
                     "opinions": [], "cites": [], "cited_by": []}
    for (cid, aid, kind, author, joined, text_key) in conn.execute(
            "SELECT id, artifact_id, kind, author, joined_by, text_key FROM component"
            " ORDER BY artifact_id, ord"):
        if aid in arts:
            arts[aid]["opinions"].append({"id": cid, "kind": kind, "author": author,
                                          "joined_by": joined, "text": text_key})
    titles = dict(conn.execute("SELECT id, title FROM artifact").fetchall())
    seen = set()
    for (from_comp, from_art, to, pin, raw) in conn.execute(
            "SELECT c.from_component, f.artifact_id, c.to_artifact, c.pinpoint, c.raw_text"
            " FROM citation c JOIN component f ON f.id = c.from_component"
            " ORDER BY f.artifact_id, f.ord, c.id"):
        if from_art in arts:
            arts[from_art]["cites"].append({"from": from_comp, "to": to, "pin": pin, "raw": raw,
                                            "title": titles.get(to)})
        if to in arts and (to, from_art) not in seen and len(arts[to]["cited_by"]) < MAX_CITED_BY:
            seen.add((to, from_art))
            arts[to]["cited_by"].append({"id": from_art, "title": titles.get(from_art)})
    conn.commit()
    return arts


def detail_key(artifact_id: str) -> str:
    return f"a/{artifact_id}.json"


def write_details(conn, r2: R2) -> int:
    """Reader files live at stable keys and are only re-uploaded when they change."""
    details = build_details(conn)
    for aid, d in details.items():
        r2.put_text(detail_key(aid), json.dumps(d, separators=(",", ":"), default=str),
                    content_type="application/json")
    return len(details)


def run(conn, r2: R2 | None = None) -> str:
    r2 = r2 or R2()
    version = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H%M%SZ")
    prefix = f"v/{version}"
    catalog = build_catalog(conn)
    readers = write_details(conn, r2)
    print(f"Reader files: {readers}", flush=True)
    r2.put_json(f"{prefix}/catalog.json", catalog)
    r2.put_json(
        "manifest.json",
        {"version": version, "prefix": prefix, "artifacts": len(catalog)},
        cache_control="public, max-age=60",
    )
    return version

"""Export the database to static files in R2 for the front end.

Each publish writes under a new version prefix, then replaces manifest.json, so a
reader never sees a half-written update. Rolling back means pointing the manifest
at an older prefix.
"""

import datetime as dt
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


def run(conn, r2: R2 | None = None) -> str:
    r2 = r2 or R2()
    version = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H%M%SZ")
    prefix = f"v/{version}"
    catalog = build_catalog(conn)
    r2.put_json(f"{prefix}/catalog.json", catalog)
    r2.put_json(
        "manifest.json",
        {"version": version, "prefix": prefix, "artifacts": len(catalog)},
        cache_control="public, max-age=60",
    )
    return version

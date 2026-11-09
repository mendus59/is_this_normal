"""Free: store the citations in executive documents and laws, so the U.S. Code sections
they cite get loaded (`itn ingest cited-usc`) and the scoring stages can read them.

Rulings get their citations when they are ingested. Executive documents and laws have a
single text, so each gets one component of kind "text" pointing at the artifact's own
text, and its citations are the bare "8 U.S.C. 1182(f)" forms plus anything eyecite finds
(eyecite only on texts up to EYECITE_MAX_CHARS, since it takes minutes on omnibus laws).
"""

import time

from itn import db, ids
from itn.extract import citations

EXTRACTOR = "text-v1"
TYPES = ("executive_document", "public_law")
EYECITE_MAX_CHARS = 300_000


def statute_rows(text: str, citing_date: str | None = None) -> list[dict]:
    """One row per distinct U.S. Code citation in the text, with its context."""
    rows, seen = [], set()
    for m in citations._BARE_USC.finditer(text or ""):
        target = ids.usc_section(m.group(1), m.group(2))
        raw = m.group(0)
        if (target, raw) in seen:
            continue
        seen.add((target, raw))
        rows.append({"to_artifact": target, "pinpoint": None, "raw_text": raw,
                     "context_snippet": text[max(0, m.start() - citations.CONTEXT_CHARS):
                                             m.end() + citations.CONTEXT_CHARS].strip(),
                     "citing_date": citing_date})
    return rows


def rows_for(text: str, citing_date: str | None, self_id: str) -> list[dict]:
    rows = statute_rows(text, citing_date)
    if len(text) <= EYECITE_MAX_CHARS:
        have = {(r["to_artifact"], r["raw_text"]) for r in rows}
        rows += [r for r in citations.extract(text, citing_date) if (r["to_artifact"], r["raw_text"]) not in have]
    return [r for r in rows if r["to_artifact"] != self_id]


def todo(conn) -> list[tuple[str, str, object]]:
    rows = conn.execute(
        "SELECT a.id, a.text_key, a.effective_date FROM artifact a"
        " WHERE a.type = ANY(%s) AND a.in_scope AND a.text_key IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM component c WHERE c.artifact_id = a.id AND c.kind = 'text')"
        " ORDER BY a.effective_date, a.id", (list(TYPES),)).fetchall()
    conn.commit()
    return rows


def run(conn, r2, max_minutes: float = 95, **_) -> int:
    deadline = time.monotonic() + max_minutes * 60
    items = todo(conn)
    print(f"Executive documents and laws without stored citations: {len(items)}", flush=True)
    count = cites = 0
    for aid, key, date in items:
        if time.monotonic() > deadline:
            print(f"Time budget reached at {aid}; resuming next run", flush=True)
            break
        text = (r2.get_text(key) or "").replace("\x00", "")  # Postgres text cannot hold NUL bytes
        comp = {"id": ids.component(aid, 0, "text"), "artifact_id": aid, "kind": "text",
                "author": None, "joined_by": [], "ord": 0, "text_key": key}
        rows = rows_for(text, date.isoformat() if date else None, aid)
        db.upsert_component(conn, comp)
        db.replace_citations(conn, comp["id"], EXTRACTOR, rows)
        conn.commit()
        count += 1
        cites += len(rows)
        if count % 500 == 0:
            print(f"  {count} done, {cites} citations", flush=True)
    print(f"Stored citations for {count} items ({cites} citations)", flush=True)
    return count

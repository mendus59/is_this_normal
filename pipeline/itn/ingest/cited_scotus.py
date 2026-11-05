"""Supreme Court cases cited by the corpus but not in it, mostly from before 2000,
loaded from the Caselaw Access Project's U.S. Reports volumes so readers can follow a
citation to its text. https://static.case.law/

Only cases whose first page is cited are loaded. They are out of scope for scoring
(closure_depth 1): they exist to be read and cited, not judged.
"""

import time
from collections import defaultdict

from itn import db, http, ids
from itn.extract import citations
from itn.http import get
from itn.ingest import cap_scotus as cap

SOURCE = "cited_scotus"


def full_date(value: str | None) -> str | None:
    """CAP gives some early decisions only a year or a month ("1786-03")."""
    if not value:
        return None
    parts = (value.split("-") + ["01", "01"])[:3]
    return "-".join(p.zfill(2) for p in parts)


def wanted(conn) -> dict[int, set[int]]:
    """Volume -> first pages of Supreme Court cases cited by the corpus itself (not by
    other cited cases, which would pull in the whole U.S. Reports) that aren't loaded."""
    rows = conn.execute(
        "SELECT DISTINCT c.to_artifact FROM citation c"
        " JOIN component f ON f.id = c.from_component"
        " JOIN artifact src ON src.id = f.artifact_id AND src.closure_depth = 0"
        " LEFT JOIN artifact a ON a.id = c.to_artifact"
        " WHERE c.to_artifact LIKE %s AND a.id IS NULL", ("scotus/%-us-%",)).fetchall()
    conn.commit()
    out: dict[int, set[int]] = defaultdict(set)
    for (aid,) in rows:
        vol, _, page = aid.removeprefix("scotus/").partition("-us-")
        if vol.isdigit() and page.isdigit():
            out[int(vol)].add(int(page))
    return out


def run(conn, r2=None, fetch_text: bool = True, max_minutes: float = 170, **_) -> int:
    deadline = time.monotonic() + max_minutes * 60
    todo = wanted(conn)
    print(f"Cited Supreme Court cases not yet loaded: {sum(map(len, todo.values()))} "
          f"in {len(todo)} volumes", flush=True)
    count = 0
    with http.client(timeout=300) as client:
        for vol in sorted(todo):
            if time.monotonic() > deadline:
                print(f"Time budget reached before volume {vol}; resuming next run", flush=True)
                break
            resp = get(client, f"{cap.BASE}/{vol}.zip")
            if resp.status_code == 404:
                continue  # volumes after CAP's last one; the Court's own sources cover those
            if resp.is_error:
                print(f"CAP volume {vol}: {resp.status_code}; retrying next run", flush=True)
                continue
            pages, n = todo[vol], 0
            for case in cap.cases_in_zip(resp.content):
                cite = cap.us_cite(case)
                if not cite or int(cite[1]) not in pages:
                    continue
                case["decision_date"] = full_date(case.get("decision_date"))
                artifact = cap.to_artifact(case, *cite)
                artifact["in_scope"] = False
                artifact["closure_depth"] = 1
                comps = cap.to_components(artifact["id"], case)
                artifact["metadata"]["opinions"] = len(comps)
                db.upsert_artifact(conn, artifact)
                for comp, text in comps:
                    if text and fetch_text and r2:
                        comp["text_key"] = ids.component_text_key(comp["id"])
                        r2.put_text(comp["text_key"], text)
                    db.upsert_component(conn, comp)
                    db.replace_citations(conn, comp["id"], citations.EXTRACTOR,
                                         citations.extract(text, case.get("decision_date")))
                conn.commit()
                n += 1
            count += n
            print(f"Volume {vol}: {n} of {len(pages)} cited cases loaded ({count} so far)", flush=True)
    return count

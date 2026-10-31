"""Supreme Court opinions in bulk from the Caselaw Access Project (Harvard Law School
Library), one zip per volume of the U.S. Reports. https://static.case.law/

CourtListener throttles opinion text to roughly one request a minute, which would
take weeks for the 2000-onward backlog. CAP covers the older volumes in about one
request each, and the `scotus` source fills in what CAP doesn't have.
"""

import io
import json
import re
import time
import zipfile

from itn import db, http, ids
from itn.config import SCOPE_START
from itn.extract import citations
from itn.http import get

BASE = "https://static.case.law/us"
SOURCE = "cap_scotus"
FIRST_VOLUME = 528  # October Term 1999; cases decided before 2000-01-01 are skipped
_US_CITE = re.compile(r"^(\d+)\s+U\.\s?S\.\s+(\d+)$")
# CAP opinion types, mapped to the kinds the CourtListener source uses.
KINDS = {"majority": "majority", "plurality": "plurality", "concurrence": "concurrence",
         "dissent": "dissent", "concurring-in-part-and-dissenting-in-part": "concurrence_in_part",
         "per-curiam": "majority", "unanimous": "unanimous"}
# U.S. Reports volumes also print thousands of one-line orders ("Certiorari denied.")
# as cases. A decision with an actual opinion runs well past this many characters.
MIN_OPINION_CHARS = 3000
_AUTHOR = re.compile(r"^(mr\.\s+)?(chief\s+)?justice\s+", re.I)


def us_cite(case: dict) -> tuple[str, str] | None:
    for c in case.get("citations") or []:
        m = _US_CITE.match((c.get("cite") or "").strip())
        if m:
            return m.group(1), m.group(2)
    return None


def clean_author(author: str | None) -> str | None:
    if not author:
        return None
    name = _AUTHOR.sub("", author.strip())
    name = re.split(r",|\s+(delivered|announced|concurring|dissenting|filed)\b", name, maxsplit=1)[0]
    return name.strip(" .") or None


def to_artifact(case: dict, vol: str, page: str) -> dict:
    date = case.get("decision_date")
    body = case.get("casebody") or {}
    return {
        "id": ids.scotus_case(vol, page),
        "type": "scotus_case",
        "subtype": "opinion",
        "title": case.get("name_abbreviation") or case.get("name"),
        "effective_date": date,
        "in_scope": bool(date and date >= SCOPE_START),
        "closure_depth": 0,
        "sources": [
            {"label": "Justia", "url": f"https://supreme.justia.com/cases/federal/us/{vol}/{page}/"},
        ],
        "official_summary": None,
        "text_key": None,
        "text_sha256": None,
        "metadata": {
            "us_cite": f"{vol} U.S. {page}",
            "docket_number": case.get("docket_number"),
            "cap_id": case.get("id"),
            "cap_file": f"us/{vol}.zip:{case.get('file_name')}",
            "judges": body.get("judges"),
            "source": "cap",
        },
    }


def to_components(aid: str, case: dict) -> list[tuple[dict, str]]:
    out = []
    for i, op in enumerate((case.get("casebody") or {}).get("opinions") or []):
        kind = KINDS.get((op.get("type") or "").lower(), "other")
        out.append(({"id": ids.component(aid, i, kind), "artifact_id": aid, "kind": kind,
                     "author": clean_author(op.get("author")), "joined_by": [], "ord": i,
                     "text_key": None}, (op.get("text") or "").strip()))
    return out


def is_decision(case: dict) -> bool:
    """A Court opinion of real length; a long dissent from a denial of certiorari is
    still an order."""
    opinions = (case.get("casebody") or {}).get("opinions") or []
    return any(KINDS.get((op.get("type") or "").lower()) in ("majority", "plurality", "unanimous")
               and len(op.get("text") or "") >= MIN_OPINION_CHARS for op in opinions)


def cases_in_zip(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in sorted(z.namelist()):
            if re.search(r"(^|/)(cases|json)/[^/]+\.json$", name):
                case = json.loads(z.read(name))
                case.setdefault("file_name", name.rsplit("/", 1)[-1][:-5])
                yield case


def volumes(client) -> list[int]:
    resp = get(client, f"{BASE}/VolumesMetadata.json")
    resp.raise_for_status()
    nums = []
    for v in resp.json():
        n = str(v.get("volume_number") or v.get("volume") or "")
        if n.isdigit() and int(n) >= FIRST_VOLUME:
            nums.append(int(n))
    return sorted(set(nums))


def run(conn, r2=None, fetch_text: bool = True, max_minutes: float = 95, **_) -> int:
    deadline = time.monotonic() + max_minutes * 60
    cursor = db.get_cursor(conn, SOURCE) or {}
    # v2: earlier runs loaded orders as cases; re-reading those volumes takes them out of scope.
    done = set(cursor.get("done_volumes_v2", []))
    count = 0
    with http.client(timeout=300) as client:
        todo = [v for v in volumes(client) if v not in done]
        if not todo:
            print("CAP: no new volumes", flush=True)
        for vol in todo:
            if time.monotonic() > deadline:
                print(f"Time budget reached before CAP volume {vol}; resuming next run", flush=True)
                break
            resp = get(client, f"{BASE}/{vol}.zip")
            if resp.is_error:
                print(f"CAP volume {vol}: {resp.status_code}; retrying next run", flush=True)
                break
            n, orders, before = 0, set(), 0
            for case in cases_in_zip(resp.content):
                cite = us_cite(case)
                date = case.get("decision_date") or ""
                if not cite or date < SCOPE_START:
                    before += 1
                    continue
                if not is_decision(case):
                    orders.add(ids.scotus_case(*cite))
                    continue
                artifact = to_artifact(case, *cite)
                comps = to_components(artifact["id"], case)
                artifact["metadata"]["opinions"] = len(comps)
                db.upsert_artifact(conn, artifact)
                for comp, text in comps:
                    if text and fetch_text and r2:
                        comp["text_key"] = ids.component_text_key(comp["id"])
                        r2.put_text(comp["text_key"], text)
                    db.upsert_component(conn, comp)
                    db.replace_citations(conn, comp["id"], citations.EXTRACTOR,
                                         citations.extract(text, date))
                conn.commit()
                orders.discard(artifact["id"])
                n += 1
            if orders:
                conn.execute(
                    "UPDATE artifact SET in_scope = false, metadata = metadata || '{\"order\": true}'"
                    " WHERE id = ANY(%s) AND metadata->>'source' = 'cap'", (sorted(orders),))
            count += n
            done.add(vol)
            db.set_cursor(conn, SOURCE, {"done_volumes_v2": sorted(done)})
            conn.commit()
            print(f"CAP volume {vol}: {n} decisions ({count} so far); {len(orders)} order pages "
                  f"and {before} pre-2000 entries skipped", flush=True)
    return count

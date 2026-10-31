"""Supreme Court opinions in bulk from the Caselaw Access Project (Harvard Law School
Library), one zip per volume of the U.S. Reports. https://static.case.law/

CourtListener throttles opinion text to roughly one request a minute, which would
take weeks for the 2000-onward backlog. CAP covers the older volumes in about one
request each, and the `scotus` source fills in what CAP doesn't have.
"""

import io
import json
import re
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


def case_files(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [n for n in sorted(z.namelist()) if re.search(r"(^|/)(cases|json)/[^/]+\.json$", n)]


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


def run(conn, r2=None, fetch_text: bool = True, **_) -> int:
    cursor = db.get_cursor(conn, SOURCE) or {}
    done = set(cursor.get("done_volumes", []))
    count = 0
    with http.client(timeout=300) as client:
        todo = [v for v in volumes(client) if v not in done]
        if not todo:
            print("CAP: no new volumes", flush=True)
        for vol in todo:
            resp = get(client, f"{BASE}/{vol}.zip")
            if resp.is_error:
                print(f"CAP volume {vol}: {resp.status_code}; retrying next run", flush=True)
                break
            try:
                files = case_files(resp.content)
            except zipfile.BadZipFile:
                files = []
            if not files:
                print(f"CAP volume {vol}: no case files ({len(resp.content)} bytes, "
                      f"{resp.headers.get('content-type')}): {resp.content[:200]!r}", flush=True)
                continue
            n, why = 0, {"no_us_cite": 0, "before_2000": 0, "no_opinions": 0}
            for case in cases_in_zip(resp.content):
                cite = us_cite(case)
                date = case.get("decision_date") or ""
                comps_raw = (case.get("casebody") or {}).get("opinions") or []
                if not cite or date < SCOPE_START or not comps_raw:
                    why["no_us_cite" if not cite else "before_2000" if date < SCOPE_START else "no_opinions"] += 1
                    if n == 0 and sum(why.values()) == 1:
                        print(f"CAP volume {vol} sample: keys={sorted(case)[:20]} "
                              f"citations={case.get('citations')} date={date!r}", flush=True)
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
                n += 1
            count += n
            done.add(vol)
            db.set_cursor(conn, SOURCE, {"done_volumes": sorted(done)})
            conn.commit()
            print(f"CAP volume {vol}: {n} cases ({count} so far), skipped {why}", flush=True)
    return count

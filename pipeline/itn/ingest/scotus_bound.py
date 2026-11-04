"""Supreme Court opinions cut from the Court's bound volumes and preliminary prints of
the U.S. Reports, for terms the Court's slip opinion lists no longer cover.

The list of cases, with each one's U.S. Reports citation, comes from Oyez's free API
(https://api.oyez.org). The volumes come from https://www.supremecourt.gov/opinions/USReports.aspx.
Each printed page carries its U.S. Reports page number, which locates a case in the PDF;
a case ends where the next case in the same volume begins.
"""

import datetime as dt
import io
import re
import time
from collections import Counter
from urllib.parse import urljoin

from pypdf import PdfReader

from itn import db, http, ids
from itn.extract import citations
from itn.http import get
from itn.ingest import supremecourt_gov as sc

OYEZ = "https://api.oyez.org"
VOLUMES_PAGE = f"{sc.SITE}/opinions/USReports.aspx"
SOURCE = "scotus_bound"
FIRST_TERM, LAST_TERM = 2014, 2018
DELAY_SECONDS = 1.0
_VOLUME_PDF = re.compile(r"""href=["']([^"']*?(\d{3})(?:bv|US\d?PP)[^"']*\.pdf)["']""", re.I)
_PAGE_NUMBER = re.compile(r"(?:^|\s)(\d{1,4})(?:\s|$)")


def oyez_cases(client, term: int) -> list[dict]:
    resp = get(client, f"{OYEZ}/cases", params={"per_page": 0, "filter": f"term:{term}"})
    resp.raise_for_status()
    return resp.json()


def decided_on(case: dict) -> str | None:
    for event in case.get("timeline") or []:
        if (event or {}).get("event") == "Decided" and event.get("dates"):
            return dt.datetime.fromtimestamp(event["dates"][0], dt.timezone.utc).date().isoformat()
    return None


def to_row(case: dict) -> dict | None:
    """The fields the supremecourt.gov loader uses, or None without a U.S. Reports page."""
    cite = case.get("citation") or {}
    vol, page = str(cite.get("volume") or ""), str(cite.get("page") or "")
    date = decided_on(case)
    if not (vol.isdigit() and page.isdigit() and date):
        return None
    docket = (case.get("docket_number") or "").strip()
    return {"date": date, "docket": docket or f"{vol}-us-{page}", "name": case.get("name") or "",
            "us_cite": (vol, page), "oyez": case.get("href")}


def volume_pdfs(page: str, base: str = VOLUMES_PAGE) -> dict[str, list[str]]:
    """Volume number -> PDF URLs (a bound volume, or a preliminary print's parts)."""
    out: dict[str, list[str]] = {}
    for href, vol in _VOLUME_PDF.findall(page):
        url = urljoin(base, href)
        if url not in out.setdefault(vol, []):
            out[vol].append(url)
    return out


def page_number(text: str) -> int | None:
    """The printed U.S. Reports page number from a page's running head."""
    for line in text.splitlines()[:3]:
        line = line.strip()
        if line.startswith("Cite as:"):
            line = re.sub(r"Cite as: \d+ U\. ?S\. \d+ \(\d{4}\)", " ", line)
        m = _PAGE_NUMBER.search(line)
        if m:
            return int(m.group(1))
    return None


def page_offset(numbers: list[int | None]) -> int | None:
    """PDF index minus printed page number, taken as the most common difference."""
    diffs = Counter(i - n for i, n in enumerate(numbers, start=1) if n)
    if not diffs:
        return None
    offset, hits = diffs.most_common(1)[0]
    return offset if hits >= max(5, len(numbers) // 10) else None


def run(conn, r2=None, fetch_text: bool = True, max_minutes: float = 170, **_) -> int:
    deadline = time.monotonic() + max_minutes * 60
    cursor = db.get_cursor(conn, SOURCE) or {}
    done_vols = set(cursor.get("done_volumes", []))
    count = 0
    with http.client(timeout=600) as client:
        rows: list[dict] = []
        for term in range(FIRST_TERM, LAST_TERM + 1):
            cases = oyez_cases(client, term)
            term_rows = [r for r in (to_row(c) for c in cases) if r]
            print(f"Oyez term {term}: {len(cases)} cases, {len(term_rows)} with a U.S. Reports page",
                  flush=True)
            rows += term_rows
        by_vol: dict[str, list[dict]] = {}
        for r in rows:
            by_vol.setdefault(r["us_cite"][0], []).append(r)
        resp = get(client, VOLUMES_PAGE)
        resp.raise_for_status()
        pdfs = volume_pdfs(resp.text, str(resp.url))
        for vol in sorted(by_vol):
            if vol in done_vols:
                continue
            vol_rows = sorted(by_vol[vol], key=lambda r: int(r["us_cite"][1]))
            todo = [r for r in vol_rows if not sc.already_loaded(conn, r["docket"], r["us_cite"])]
            if not todo:
                done_vols.add(vol)
                continue
            print(f"Volume {vol}: {len(todo)} of {len(vol_rows)} cases to load; PDFs {pdfs.get(vol)}",
                  flush=True)
            starts = sorted({int(r["us_cite"][1]) for r in vol_rows})
            complete = True
            for url in pdfs.get(vol, []):
                if time.monotonic() > deadline:
                    print(f"Time budget reached at volume {vol}; resuming next run", flush=True)
                    return count
                try:
                    time.sleep(DELAY_SECONDS)
                    pdf = get(client, url)
                    pdf.raise_for_status()
                    reader = PdfReader(io.BytesIO(pdf.content))
                    numbers = [page_number(p.extract_text() or "") for p in reader.pages]
                except Exception as err:  # noqa: BLE001 - a bad download defers the volume
                    print(f"Volume {vol}: skipping {url} for now: {str(err)[:200]}", flush=True)
                    complete = False
                    continue
                offset = page_offset(numbers)
                if offset is None:
                    print(f"Volume {vol}: no page numbers found in {url}; sample "
                          f"{[(reader.pages[i].extract_text() or '')[:120] for i in (10, 11)]}",
                          flush=True)
                    complete = False
                    continue
                first, last = 1 - offset, len(reader.pages) - offset
                for row in todo:
                    page = int(row["us_cite"][1])
                    if not first <= page <= last or row.get("loaded"):
                        continue
                    nxt = next((s for s in starts if s > page), None)
                    end = nxt + offset if nxt and nxt <= last else None
                    texts = sc.print_pages(reader, page + offset, end)
                    syllabus, parts = sc.split_opinions(texts)
                    if not parts:
                        print(f"Skipping {row['docket']}: no opinion text found", flush=True)
                        continue
                    conn = load(conn, r2, fetch_text, {**row, "pdf": url, "page": page + offset},
                                syllabus, parts)
                    row["loaded"] = True
                    count += 1
                print(f"Volume {vol}: {url} done ({count} cases so far)", flush=True)
            missing = [r["docket"] for r in todo if not r.get("loaded")]
            if missing:
                print(f"Volume {vol}: not found in any PDF: {missing[:20]}", flush=True)
            if complete:
                done_vols.add(vol)
                db.set_cursor(conn, SOURCE, {"done_volumes": sorted(done_vols)})
                conn.commit()
    return count


def load(conn, r2, fetch_text: bool, row: dict, syllabus, parts):
    artifact = sc.to_artifact(row, syllabus, None)
    aid = artifact["id"]
    artifact["metadata"]["opinions"] = len(parts)
    artifact["metadata"]["oyez"] = row.get("oyez")
    artifact["metadata"]["source"] = "supremecourt.gov U.S. Reports"
    db.upsert_artifact(conn, artifact)
    for i, (label, text) in enumerate(parts):
        kind = sc.kind_of(label)
        comp = {"id": ids.component(aid, i, kind), "artifact_id": aid, "kind": kind,
                "author": sc.author_of(label), "joined_by": [], "ord": i, "text_key": None}
        if text and fetch_text and r2:
            comp["text_key"] = ids.component_text_key(comp["id"])
            r2.put_text(comp["text_key"], text)
        db.upsert_component(conn, comp)
        db.replace_citations(conn, comp["id"], citations.EXTRACTOR,
                             citations.extract(text, row["date"]))
    conn.commit()
    return conn

"""Supreme Court opinions cut from the Court's bound volumes and preliminary prints of
the U.S. Reports (https://www.supremecourt.gov/opinions/USReports.aspx), for terms the
Court's slip opinion lists no longer cover.

A case opens on a caption page naming its docket and decision date, and runs to the
next one. The printed page numbers give its U.S. Reports citation. Case names come from
Oyez's free API (https://api.oyez.org), whose citations stop at volume 574.
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


_DECIDED = re.compile(r"Decided ([A-Z][a-z]+ \d{1,2}, \d{4})")
_DOCKET = re.compile(r"\bNos?\. (\d{1,2}[–-]\d{1,5}|\d{1,2}A\d{1,4})")
GAP = ("2014-10-01", "2019-10-01")  # decision dates no other source has covered
FIRST_VOLUME, LAST_VOLUME = 574, 591


def case_start(text: str) -> dict | None:
    """Docket and decision date if this page opens a case: the caption page carries
    "No. 14–556" and "Argued ...—Decided June 26, 2015" near the top."""
    head = "\n".join(text.splitlines()[:40])
    decided, docket = _DECIDED.search(head), _DOCKET.search(head)
    if not (decided and docket):
        return None
    try:
        date = dt.datetime.strptime(decided.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None
    return {"docket": docket.group(1).replace("–", "-"), "date": date}


def run(conn, r2=None, fetch_text: bool = True, max_minutes: float = 170, **_) -> int:
    deadline = time.monotonic() + max_minutes * 60
    cursor = db.get_cursor(conn, SOURCE) or {}
    done = set(cursor.get("done_pdfs", []))
    count = 0
    with http.client(timeout=600) as client:
        names: dict[str, dict] = {}
        for term in range(FIRST_TERM, LAST_TERM + 1):
            try:
                for case in oyez_cases(client, term):
                    if case.get("docket_number"):
                        names[case["docket_number"].strip()] = case
            except Exception as err:  # noqa: BLE001 - names are a nicety; dockets still work
                print(f"Oyez term {term} unavailable: {str(err)[:200]}", flush=True)
        print(f"Oyez: {len(names)} case names", flush=True)
        resp = get(client, VOLUMES_PAGE)
        resp.raise_for_status()
        pdfs = volume_pdfs(resp.text, str(resp.url))
        for vol in sorted(v for v in pdfs if FIRST_VOLUME <= int(v) <= LAST_VOLUME):
            for url in pdfs[vol]:
                import os
                check = os.environ.get("ITN_BOUND_CHECK")
                if check and vol not in check.split(","):
                    continue
                if url in done and not check:
                    continue
                if time.monotonic() > deadline:
                    print(f"Time budget reached at volume {vol}; resuming next run", flush=True)
                    return count
                try:
                    time.sleep(DELAY_SECONDS)
                    pdf = get(client, url)
                    pdf.raise_for_status()
                    reader = PdfReader(io.BytesIO(pdf.content))
                    texts = [p.extract_text() or "" for p in reader.pages]
                except Exception as err:  # noqa: BLE001 - a bad download is retried next run
                    print(f"Volume {vol}: skipping {url} for now: {str(err)[:200]}", flush=True)
                    continue
                offset = page_offset([page_number(t) for t in texts])
                starts = [(i, c) for i, t in enumerate(texts) if (c := case_start(t))]
                n = 0
                if check:
                    for i, case in starts:
                        print(vol, case["docket"], "caption#", page_number(texts[i]), "offset#",
                              (i + 1 - offset) if offset is not None else None,
                              repr(texts[i][:90]), flush=True)
                    continue
                for k, (i, case) in enumerate(starts):
                    if not GAP[0] <= case["date"] < GAP[1]:
                        continue
                    page = (i + 1 - offset) if offset is not None else page_number(texts[i])
                    cite = (vol, str(page)) if page and page > 0 else None
                    if sc.already_loaded(conn, case["docket"], cite):
                        continue
                    end = starts[k + 1][0] if k + 1 < len(starts) else len(texts)
                    syllabus, parts = sc.split_opinions(texts[i:end])
                    if not parts:
                        continue
                    oyez = names.get(case["docket"]) or {}
                    row = {**case, "name": oyez.get("name") or f"No. {case['docket']}",
                           "pdf": url, "page": i + 1, "oyez": oyez.get("href")}
                    if cite:
                        row["us_cite"] = cite
                    conn = load(conn, r2, fetch_text, row, syllabus, parts)
                    n += 1
                count += n
                print(f"Volume {vol}: {url}: {len(starts)} cases found, {n} loaded "
                      f"(page offset {offset})", flush=True)
                if not starts:
                    print(f"  sample page: {texts[min(30, len(texts) - 1)][:300]!r}", flush=True)
                done.add(url)
                db.set_cursor(conn, SOURCE, {"done_pdfs": sorted(done)})
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

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


_HEAD_NUMBER = re.compile(
    r"(\d{1,4})\s*OCTOBER\s+TERM,?\s*\d{4}|OCTOBER\s+TERM,?\s*\d{4}\s+(\d{1,4})\b"
    r"|Cite as: \d+ U\. ?S\. \d+ \(\d{4}\)\s+(\d{1,4})\b")


def page_number(text: str) -> int | None:
    """The printed U.S. Reports page number from a page's running head, which reads
    "644 OCTOBER TERM, 2014" or "OCTOBER TERM, 2014 383". A volume's first case opens
    on page 1 under "CASES ADJUDGED"."""
    head = " ".join(text.splitlines()[:3])
    if "CASES ADJUDGED" in head:
        return 1
    m = _HEAD_NUMBER.search(head)
    if not m:
        return None
    return int(next(g for g in m.groups() if g))


def page_offset(numbers: list[int | None]) -> int | None:
    """PDF index minus printed page number, taken as the most common difference."""
    diffs = Counter(i - n for i, n in enumerate(numbers, start=1) if n)
    if not diffs:
        return None
    offset, hits = diffs.most_common(1)[0]
    return offset if hits >= max(5, len(numbers) // 10) else None


_DECIDED = re.compile(r"Decided ([A-Z][a-z]+ \d{1,2}, \d{4})")
_DOCKET = re.compile(r"\bNos?\. (\d{1,2}[–-]\d{1,5}|\d{1,2}A\d{1,4})")
GAP = ("2014-01-01", "2020-10-01")  # decision dates no other source fully covers
# (case.law ends partway through volume 572, so the spring 2014 rulings in 573 need this)
FIRST_VOLUME, LAST_VOLUME = 572, 591


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
    # v2: page numbers were read from the wrong part of the running head before.
    # v4: a case loaded under a wrong U.S. Reports page by another source matched by
    # docket number and was skipped; it is now refiled under the bound volume's page.
    # v5: mixed-case opinion labels ("Breyer, J., dissenting") weren't recognized, so
    # pluralities and per curiams were skipped and dissents ran into the majority.
    # This source's own cases are reloaded. v6: the window now runs through OT2019
    # (volumes 589-591), whose short per curiams the slip-opinion loader also missed.
    done = set(cursor.get("done_pdfs_v6", []))
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
                if url in done:
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
                for k, (i, case) in enumerate(starts):
                    if not GAP[0] <= case["date"] < GAP[1]:
                        continue
                    page = page_number(texts[i]) or ((i + 1 - offset) if offset is not None else None)
                    cite = (vol, str(page)) if page and page > 0 else None
                    drop_misfiled(conn, case["docket"], ids.scotus_case(*cite) if cite else None)
                    sc.claim_cite(conn, case["docket"], cite)
                    if cite:
                        refile(conn, case["docket"], cite)
                    if sc.already_loaded(conn, case["docket"], cite) and not ours(conn, cite):
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
                db.set_cursor(conn, SOURCE, {"done_pdfs_v6": sorted(done)})
                conn.commit()
    return count


def drop_misfiled(conn, docket: str, right_id: str | None) -> None:
    """Remove this source's copies of a case filed under a wrong citation by earlier runs."""
    cur = conn.execute(
        "DELETE FROM artifact WHERE metadata->>'source' = %s AND metadata->>'docket_number' = %s"
        " AND id IS DISTINCT FROM %s RETURNING id",
        (BOUND_SOURCE, docket, right_id))
    gone = [r[0] for r in cur.fetchall()]
    conn.commit()
    if gone:
        print(f"Removed {docket} filed as {gone}; refiling as {right_id}", flush=True)


BOUND_SOURCE = "supremecourt.gov U.S. Reports"


def ours(conn, cite: tuple[str, str] | None) -> bool:
    """True if the case at this citation came from the Court's own PDFs (bound volumes,
    preliminary prints or slip opinions), whose opinions were split with the same
    labels, so a re-read may replace it."""
    if not cite:
        return False
    row = conn.execute("SELECT metadata->>'source' FROM artifact WHERE id = %s",
                       (ids.scotus_case(*cite),)).fetchone()
    conn.commit()
    return bool(row and row[0] in (BOUND_SOURCE, "supremecourt.gov"))


def refile(conn, docket: str, cite: tuple[str, str]) -> None:
    """Move a case another source filed under a different U.S. Reports page to the page
    its bound volume prints. The bound volume is the official citation."""
    right = ids.scotus_case(*cite)
    rows = conn.execute(
        "SELECT id FROM artifact WHERE type = 'scotus_case' AND id ~ '^scotus/[0-9]+-us-[0-9]+$'"
        " AND id <> %s AND regexp_replace(metadata->>'docket_number', '^Nos?\\.?\\s*', '') = %s",
        (right, docket)).fetchall()
    conn.commit()
    for (old,) in rows:
        result = db.rekey_artifact(conn, old, right, {"us_cite": f"{cite[0]} U.S. {cite[1]}"})
        conn.commit()
        print(f"Refiled {docket}: {old} {result} -> {right}", flush=True)


def load(conn, r2, fetch_text: bool, row: dict, syllabus, parts):
    artifact = sc.to_artifact(row, syllabus, None)
    aid = artifact["id"]
    artifact["metadata"]["opinions"] = len(parts)
    artifact["metadata"]["oyez"] = row.get("oyez")
    artifact["metadata"]["source"] = BOUND_SOURCE
    db.upsert_artifact(conn, artifact)
    # A reload replaces every opinion; drop ones an earlier split left behind.
    conn.execute("DELETE FROM component WHERE artifact_id = %s", (aid,))
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

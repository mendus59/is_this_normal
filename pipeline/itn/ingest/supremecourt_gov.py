"""Supreme Court opinions and dockets from the Court's own website, for the terms the
Caselaw Access Project doesn't cover. https://www.supremecourt.gov/opinions/

Each term has a slip opinion table linking one PDF per case. The PDF holds the
syllabus and every opinion; its running page headers ("Opinion of the Court",
"THOMAS, J., dissenting") say which opinion each page belongs to, so the text is
split on them. Cases are keyed by docket number until a U.S. Reports page exists.

For October Terms 2017-2019 the table links each case to a page of a preliminary
print (one PDF per part of a U.S. Reports volume) and gives its citation. Those
cases are cut from the print, ending where the next listed case in it begins, and
are keyed by citation. Terms 2014-2016 have no table and are left to CourtListener.
"""

import datetime as dt
import html
import io
import re
import time
from urllib.parse import urljoin

from pypdf import PdfReader

from itn import db, http, ids
from itn.extract import citations
from itn.http import get

SITE = "https://www.supremecourt.gov"
SOURCE = "supremecourt_gov"
FIRST_TERM = 2014  # CAP's U.S. Reports volumes end during October Term 2013
DELAY_SECONDS = 1.0

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_PDF = re.compile(r"""href=["']([^"'#]*/opinions/[^"'#]+\.pdf)(?:#page=(\d+))?["']""", re.I)
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
_US_CITE = re.compile(r"^(\d+) U\. ?S\. (\d+)$")

# Running headers that name the opinion a page belongs to.
_LABEL = re.compile(
    r"^(Syllabus|Per Curiam|Opinion of the Court|Opinion of (?:the )?[A-Z' ]+, (?:C\. )?J\.|"
    r"(?:[A-Z' ]+), (?:C\. )?J\., (?:concurring|dissenting|statement|respecting)[^\n]*|"
    r"Statement of [A-Z' ]+, (?:C\. )?J\.)$")
_DROP = re.compile(r"^(Cite as: .*|\d{1,3}|[A-Z0-9 .,'&v-]+ v\. [A-Z0-9 .,'&-]+)$")


def term_of(day: dt.date) -> int:
    """October Term year: cases argued from October belong to that year's term."""
    return day.year if day.month >= 10 else day.year - 1


def parse_term_table(page: str, base: str = f"{SITE}/opinions/slipopinion/") -> list[dict]:
    """Rows of a slip opinion table: date, docket, name, author column, PDF URL."""
    rows = []
    for row in _ROW.findall(page):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in _CELL.findall(row)]
        pdf = _PDF.search(row)
        if not pdf or len(cells) < 4:
            continue
        date = next((c for c in cells if _DATE.match(c)), None)
        docket = next((c for c in cells if re.fullmatch(r"\d+-\d+|\d+A\d+|\d+,? ?Orig\.?", c)), None)
        if not date or not docket:
            continue
        m, d, y = (int(x) for x in _DATE.match(date).groups())
        i = cells.index(docket)
        url = urljoin(base, pdf.group(1))
        row = {"date": dt.date(y if y >= 100 else 2000 + y, m, d).isoformat(), "docket": docket,
               "name": cells[i + 1] if i + 1 < len(cells) else docket,
               "pdf": url}
        if pdf.group(2):
            row["page"] = int(pdf.group(2))
        cite = next((_US_CITE.match(c) for c in cells if _US_CITE.match(c)), None)
        if cite:
            row["us_cite"] = (cite.group(1), cite.group(2))
        rows.append(row)
    return rows


def kind_of(label: str) -> str:
    low = label.lower()
    if low in ("opinion of the court", "per curiam"):
        return "majority"
    if "concurring in part and dissenting" in low or "dissenting in part" in low:
        return "concurrence_in_part"
    if "dissenting" in low:
        return "dissent"
    if "concurring" in low:
        return "concurrence"
    if low.startswith("opinion of"):
        return "plurality"
    return "other"


def author_of(label: str) -> str | None:
    m = re.search(r"([A-Z][A-Z' ]+), (?:C\. )?J\.", label)
    if not m:
        return "Per Curiam" if label.lower() == "per curiam" else None
    return m.group(1).strip().title().replace("'S", "'s")


def split_opinions(pages: list[str]) -> tuple[str | None, list[tuple[str, str]]]:
    """(syllabus, [(label, text), ...]) from the text of each PDF page.

    An opinion's first page has no running header, only the court caption, so it
    takes the label of the page after it."""
    labels, bodies, starts = [], [], []
    for page in pages:
        lines = [ln.strip() for ln in page.splitlines()]
        label = next((ln for ln in lines[:8] if _LABEL.match(ln)), None)
        labels.append(label)
        starts.append(any("SUPREME COURT OF THE UNITED STATES" in ln for ln in lines[:15]))
        bodies.append("\n".join(ln for ln in lines if ln and ln != label and not _DROP.match(ln)))
    for i in range(len(labels)):
        if labels[i] is None and starts[i]:
            labels[i] = next((lb for lb in labels[i + 1:] if lb), None)
    parts: list[list] = []
    for i, (label, body) in enumerate(zip(labels, bodies)):
        label = label or (parts[-1][0] if parts else "Opinion of the Court")
        if not parts or parts[-1][0] != label or (starts[i] and label != "Syllabus"):
            parts.append([label, []])
        parts[-1][1].append(body)
    syllabus = None
    out = []
    for label, chunks in parts:
        text = "\n".join(chunks).strip()
        if label == "Syllabus":
            syllabus = text
        else:
            out.append((label, text))
    return syllabus, out


def pdf_pages(data: bytes) -> list[str]:
    return [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]


def print_ranges(rows: list[dict]) -> dict[tuple[str, int], int | None]:
    """(pdf, first page) -> first page of the next case in the same print, or None
    for the last case listed in it."""
    starts: dict[str, set[int]] = {}
    for r in rows:
        if r.get("page"):
            starts.setdefault(r["pdf"], set()).add(r["page"])
    out = {}
    for pdf, pages in starts.items():
        ordered = sorted(pages)
        for a, b in zip(ordered, ordered[1:] + [None]):
            out[(pdf, a)] = b
    return out


# A preliminary print can end with orders, a table of cases, or an index.
_PRINT_TAIL = re.compile(r"^(ORDERS FROM|INDEX|TABLE OF CASES|REPORTER'S NOTE)", re.M)
MAX_CASE_PAGES = 150


def print_pages(reader, start: int, end: int | None) -> list[str]:
    """Text of one case in a preliminary print; pages are 1-based like #page=N."""
    stop = min(end - 1 if end else start + MAX_CASE_PAGES - 1, len(reader.pages))
    pages = []
    for n in range(start, stop + 1):
        text = reader.pages[n - 1].extract_text() or ""
        if pages and end is None and _PRINT_TAIL.search("\n".join(text.splitlines()[:6])):
            break
        pages.append(text)
    return pages


def to_artifact(row: dict, syllabus: str | None, docket: dict | None) -> dict:
    cite = row.get("us_cite")
    aid = ids.scotus_case(*cite) if cite else ids.scotus_docket(row["docket"])
    return {
        "id": aid,
        "type": "scotus_case",
        "subtype": "opinion",
        "title": row["name"],
        "effective_date": row["date"],
        "in_scope": True,
        "closure_depth": 0,
        "sources": [
            {"label": "Supreme Court opinion (PDF)",
             "url": row["pdf"] + (f"#page={row['page']}" if row.get("page") else "")},
            {"label": "Supreme Court docket",
             "url": f"{SITE}/search.aspx?filename=/docket/docketfiles/html/public/{row['docket']}.html"},
        ],
        "official_summary": syllabus[:4000] if syllabus else None,
        "text_key": None,
        "text_sha256": None,
        "metadata": {
            "docket_number": row["docket"],
            "slip_opinion_pdf": row["pdf"] + (f"#page={row['page']}" if row.get("page") else ""),
            "us_cite": f"{cite[0]} U.S. {cite[1]}" if cite else None,
            "source": "supremecourt.gov",
            **({"docket": docket} if docket else {}),
        },
    }


def docket_summary(data: dict) -> dict:
    """The parts of the Court's docket JSON worth keeping on the artifact."""
    events = data.get("ProceedingsandOrder") or []
    return {
        "docketed": data.get("DocketedDate"),
        "lower_court": data.get("LowerCourt"),
        "lower_court_case": data.get("LowerCourtCaseNumbers"),
        "lower_court_decision": data.get("LowerCourtDecision"),
        "petitioner": data.get("PetitionerTitle"),
        "respondent": data.get("RespondentTitle"),
        "events": [{"date": e.get("Date"), "text": (e.get("Text") or "")[:500]} for e in events],
    }


def claim_cite(conn, docket: str, cite: tuple[str, str] | None) -> None:
    """A case first loaded under its docket number moves to its U.S. Reports id once a
    source shows the citation, so links to that citation reach it and it isn't listed twice."""
    if not cite or not docket:
        return
    old, new = ids.scotus_docket(docket), ids.scotus_case(*cite)
    result = db.rekey_artifact(conn, old, new, {"us_cite": f"{cite[0]} U.S. {cite[1]}"})
    if result != "missing":
        print(f"{old}: {result} -> {new}", flush=True)


def already_loaded(conn, docket: str, cite: tuple[str, str] | None = None) -> bool:
    """True if this case came in already, from here, CAP, or CourtListener."""
    cite_id = ids.scotus_case(*cite) if cite else None
    row = conn.execute(
        "SELECT 1 FROM artifact WHERE type = 'scotus_case' AND metadata ? 'opinions'"
        " AND (id = %s OR id = %s"
        " OR regexp_replace(metadata->>'docket_number', '^No\\.?\\s*', '') = %s)",
        (ids.scotus_docket(docket), cite_id, docket)).fetchone()
    conn.commit()
    return bool(row)


def describe_unparsed(page: str) -> str:
    """A short sample of a page whose table didn't parse, for fixing the parser."""
    pdf_rows = [r for r in _ROW.findall(page) if ".pdf" in r.lower()]
    hrefs = re.findall(r"""href=["']([^"']+\.pdf)["']""", page, re.I)[:3]
    sample = re.sub(r"\s+", " ", pdf_rows[0])[:600] if pdf_rows else re.sub(r"\s+", " ", page[:600])
    return f"{len(pdf_rows)} table rows with PDFs; hrefs {hrefs}; sample: {sample}"


def run(conn, r2=None, since_term: int | None = None, fetch_text: bool = True,
        max_minutes: float = 95, **_) -> int:
    deadline = time.monotonic() + max_minutes * 60
    cursor = db.get_cursor(conn, SOURCE) or {}
    current = term_of(dt.date.today())
    start = since_term or cursor.get("next_term_v2", FIRST_TERM)
    count = 0
    with http.client(timeout=300) as client:
        # Read every term's table first: a case cut from a preliminary print ends
        # where the next case in that print begins, which may be listed in another term.
        tables: dict[int, list[dict]] = {}
        for term in range(start, current + 1):
            resp = get(client, f"{SITE}/opinions/slipopinion/{term % 100:02d}")
            if resp.is_error:
                print(f"Term {term}: {resp.status_code}; retrying next run", flush=True)
                break
            rows = parse_term_table(resp.text, str(resp.url))
            print(f"Term {term}: {len(rows)} opinions listed", flush=True)
            if not rows:
                # The Court keeps slip opinion lists only from October Term 2017; older
                # ones redirect to the bound volumes page. CourtListener covers those.
                print(f"Term {term}: no slip opinion list (got {resp.url}); "
                      f"{describe_unparsed(resp.text)}", flush=True)
            tables[term] = rows
        ranges = print_ranges([r for rows in tables.values() for r in rows])
        prints: dict[str, PdfReader] = {}
        for term, rows in tables.items():
            for row in rows:
                if time.monotonic() > deadline:
                    print(f"Time budget reached in term {term}; resuming next run", flush=True)
                    return count
                claim_cite(conn, row["docket"], row.get("us_cite"))
                if already_loaded(conn, row["docket"], row.get("us_cite")):
                    continue
                try:
                    if row.get("page"):
                        if row["pdf"] not in prints:
                            time.sleep(DELAY_SECONDS)
                            pdf = get(client, row["pdf"])
                            pdf.raise_for_status()
                            prints.clear()  # one print in memory at a time
                            prints[row["pdf"]] = PdfReader(io.BytesIO(pdf.content))
                        pages = print_pages(prints[row["pdf"]], row["page"],
                                            ranges.get((row["pdf"], row["page"])))
                    else:
                        time.sleep(DELAY_SECONDS)
                        pdf = get(client, row["pdf"])
                        pdf.raise_for_status()
                        pages = pdf_pages(pdf.content)
                    syllabus, parts = split_opinions(pages)
                    time.sleep(DELAY_SECONDS)
                    dresp = get(client, f"{SITE}/RSS/Cases/JSON/{row['docket']}.json")
                    docket = docket_summary(dresp.json()) if dresp.is_success else None
                except Exception as err:  # noqa: BLE001 - network errors and malformed PDFs both just defer the case
                    print(f"Skipping {row['docket']} for now: {str(err)[:200]}", flush=True)
                    continue
                artifact = to_artifact(row, syllabus, docket)
                aid = artifact["id"]
                artifact["metadata"]["opinions"] = len(parts)
                if not parts:
                    print(f"Skipping {row['docket']}: no opinion text found", flush=True)
                    continue
                db.upsert_artifact(conn, artifact)
                for i, (label, text) in enumerate(parts):
                    kind = kind_of(label)
                    comp = {"id": ids.component(aid, i, kind), "artifact_id": aid, "kind": kind,
                            "author": author_of(label), "joined_by": [], "ord": i, "text_key": None}
                    if text and fetch_text and r2:
                        comp["text_key"] = ids.component_text_key(comp["id"])
                        r2.put_text(comp["text_key"], text)
                    db.upsert_component(conn, comp)
                    db.replace_citations(conn, comp["id"], citations.EXTRACTOR,
                                         citations.extract(text, row["date"]))
                conn.commit()
                count += 1
            # Re-scan the current term every run so new opinions are picked up.
            db.set_cursor(conn, SOURCE, {"next_term_v2": min(term + 1, current)})
            conn.commit()
            print(f"Term {term}: done ({count} cases so far)", flush=True)
    return count

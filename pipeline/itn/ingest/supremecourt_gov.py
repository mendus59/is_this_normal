"""Supreme Court opinions and dockets from the Court's own website, for the terms the
Caselaw Access Project doesn't cover. https://www.supremecourt.gov/opinions/

Each term has a slip opinion table linking one PDF per case. The PDF holds the
syllabus and every opinion; its running page headers ("Opinion of the Court",
"THOMAS, J., dissenting") say which opinion each page belongs to, so the text is
split on them. Cases are keyed by docket number until a U.S. Reports page exists.
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
_PDF = re.compile(r"""href=["']([^"']*\d\dpdf/[^"']+\.pdf)["']""", re.I)
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")

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
        rows.append({"date": dt.date(2000 + y, m, d).isoformat(), "docket": docket,
                     "name": cells[i + 1] if i + 1 < len(cells) else docket,
                     "pdf": url})
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


def to_artifact(row: dict, syllabus: str | None, docket: dict | None) -> dict:
    aid = ids.scotus_docket(row["docket"])
    return {
        "id": aid,
        "type": "scotus_case",
        "subtype": "opinion",
        "title": row["name"],
        "effective_date": row["date"],
        "in_scope": True,
        "closure_depth": 0,
        "sources": [
            {"label": "Supreme Court opinion (PDF)", "url": row["pdf"]},
            {"label": "Supreme Court docket",
             "url": f"{SITE}/search.aspx?filename=/docket/docketfiles/html/public/{row['docket']}.html"},
        ],
        "official_summary": syllabus[:4000] if syllabus else None,
        "text_key": None,
        "text_sha256": None,
        "metadata": {
            "docket_number": row["docket"],
            "slip_opinion_pdf": row["pdf"],
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


def already_loaded(conn, docket: str) -> bool:
    """True if this case came in from CAP or CourtListener under its U.S. Reports id."""
    row = conn.execute(
        "SELECT 1 FROM artifact WHERE type = 'scotus_case' AND metadata ? 'opinions'"
        " AND (id = %s OR regexp_replace(metadata->>'docket_number', '^No\\.?\\s*', '') = %s)",
        (ids.scotus_docket(docket), docket)).fetchone()
    conn.commit()
    return bool(row)


def run(conn, r2=None, fetch_text: bool = True, max_minutes: float = 95, **_) -> int:
    deadline = time.monotonic() + max_minutes * 60
    cursor = db.get_cursor(conn, SOURCE) or {}
    current = term_of(dt.date.today())
    start = cursor.get("next_term_v2", FIRST_TERM)
    count = 0
    with http.client(timeout=180) as client:
        for term in range(start, current + 1):
            resp = get(client, f"{SITE}/opinions/slipopinion/{term % 100:02d}")
            if resp.is_error:
                print(f"Term {term}: {resp.status_code}; retrying next run", flush=True)
                break
            rows = parse_term_table(resp.text, str(resp.url))
            print(f"Term {term}: {len(rows)} opinions listed", flush=True)
            if not rows:
                # The Court keeps slip opinion lists only for recent terms; older ones
                # redirect to the bound volumes page. CourtListener covers those terms.
                print(f"Term {term}: no slip opinion list (got {resp.url})", flush=True)
                continue
            for row in rows:
                if time.monotonic() > deadline:
                    print(f"Time budget reached in term {term}; resuming next run", flush=True)
                    return count
                if already_loaded(conn, row["docket"]):
                    continue
                try:
                    time.sleep(DELAY_SECONDS)
                    pdf = get(client, row["pdf"])
                    pdf.raise_for_status()
                    syllabus, parts = split_opinions(pdf_pages(pdf.content))
                    time.sleep(DELAY_SECONDS)
                    dresp = get(client, f"{SITE}/RSS/Cases/JSON/{row['docket']}.json")
                    docket = docket_summary(dresp.json()) if dresp.is_success else None
                except Exception as err:  # noqa: BLE001 - network errors and malformed PDFs both just defer the case
                    print(f"Skipping {row['docket']} for now: {str(err)[:200]}", flush=True)
                    continue
                artifact = to_artifact(row, syllabus, docket)
                aid = artifact["id"]
                artifact["metadata"]["opinions"] = len(parts)
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

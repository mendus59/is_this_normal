"""Enacted public laws from the Congress.gov API, with CRS summaries, GovInfo text,
and recorded final-passage votes for the contentiousness test.
https://api.congress.gov/ (key from api.data.gov, env DATA_GOV_API_KEY)

Congress.gov allows 5,000 requests an hour per key, so calls are paced and every
law is committed as soon as it is complete. A run that stops part way resumes
where it left off, and laws already loaded are skipped.
"""

import datetime as dt
import re
import time
import xml.etree.ElementTree as ET

import httpx

from itn import db, http, ids
from itn.config import CONTENTIOUS_THRESHOLD, SCOPE_START, require
from itn.http import get

API = "https://api.congress.gov/v3"
SOURCE = "congress_laws"
FIRST_CONGRESS = 106  # 1999-2000; laws enacted before 2000-01-01 are filtered out
# Stay under 5,000 requests per hour.
API_DELAY_SECONDS = 0.8
GOVINFO_TEXT = "https://www.govinfo.gov/content/pkg/PLAW-{c}publ{n}/html/PLAW-{c}publ{n}.htm"
GOVINFO_PDF = "https://www.govinfo.gov/content/pkg/PLAW-{c}publ{n}/pdf/PLAW-{c}publ{n}.pdf"

BILL_TYPE_URL = {"HR": "house-bill", "S": "senate-bill", "HJRES": "house-joint-resolution",
                 "SJRES": "senate-joint-resolution"}

_COUNTS = re.compile(r"(\d+)\s*-\s*(\d+)")
_PASSAGE = re.compile(
    r"on passage|passed (house|senate)|passed by|and pass|conference report|"
    r"agreed to by|concur|override|resolution agreed",
    re.I,
)
_NOT_PASSAGE = re.compile(r"cloture|motion to (proceed|table|recommit|instruct)|previous question|"
                          r"rule providing|waive", re.I)


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def current_congress(year: int) -> int:
    return (year - 1789) // 2 + 1


def is_final_passage(text: str) -> bool:
    """True for votes that pass the measure in a chamber: passage, suspension passage,
    conference reports, concurring in the other chamber's changes, veto overrides."""
    return bool(_PASSAGE.search(text)) and not _NOT_PASSAGE.search(text)


def counts_from_text(text: str) -> tuple[int, int] | None:
    m = _COUNTS.search(text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def counts_from_xml(xml: str, chamber: str) -> tuple[int, int] | None:
    """Yea and nay totals from a House Clerk or Senate roll-call XML file."""
    root = ET.fromstring(xml)
    if chamber.lower() == "house":
        yea, nay = root.findtext(".//totals-by-vote/yea-total"), root.findtext(".//totals-by-vote/nay-total")
    else:
        yea, nay = root.findtext(".//count/yeas"), root.findtext(".//count/nays")
    return (int(yea), int(nay)) if yea and nay else None


def contentious(votes: list[dict], threshold: float = CONTENTIOUS_THRESHOLD) -> bool:
    """Contentious if any recorded final-passage vote fell below the threshold yes share.
    Laws with no recorded vote (voice vote, unanimous consent) are not contentious."""
    return any(v["yeas"] / (v["yeas"] + v["nays"]) < threshold
               for v in votes if v["yeas"] + v["nays"] > 0)


def to_artifact(bill: dict, law_number: str, enacted: str | None, summary: str | None,
                votes: list[dict]) -> dict:
    congress, number = law_number.split("-")
    btype = bill["type"].upper()
    bill_slug = f"{btype.lower()}{bill['number']}"
    sources = [
        {"label": "Congress.gov",
         "url": f"https://www.congress.gov/bill/{ordinal(int(congress))}-congress/"
                f"{BILL_TYPE_URL.get(btype, 'bill')}/{bill['number']}"},
        {"label": "GovInfo (PDF)", "url": GOVINFO_PDF.format(c=congress, n=number)},
    ]
    is_contentious = contentious(votes)
    return {
        "id": ids.public_law(congress, number),
        "type": "public_law",
        "subtype": "public_law",
        "title": bill.get("title") or f"Public Law {law_number}",
        "effective_date": enacted,
        "in_scope": bool(enacted and enacted >= SCOPE_START and is_contentious),
        "closure_depth": 0,
        "sources": sources,
        "contentious": is_contentious,
        "official_summary": summary,
        "text_key": None,
        "text_sha256": None,
        "metadata": {
            "law_number": law_number,
            "bill": f"{bill_slug}-{bill['congress']}",
            "votes": votes,
            "contentious": is_contentious,
        },
    }


class CongressAPI:
    def __init__(self, client, key: str):
        self.client, self.key = client, key

    def get(self, path: str, **params) -> dict:
        time.sleep(API_DELAY_SECONDS)
        resp = get(self.client, f"{API}{path}", params={"api_key": self.key, "format": "json", **params})
        if resp.is_error:
            raise RuntimeError(f"Congress.gov API returned {resp.status_code} for {path}: {resp.text[:500]}")
        return resp.json()

    def paged(self, path: str, key: str):
        offset = 0
        while True:
            page = self.get(path, limit=250, offset=offset)
            items = page.get(key, [])
            yield from items
            if not page.get("pagination", {}).get("next") or not items:
                return
            offset += len(items)


def _strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def law_details(api: CongressAPI, client, bill: dict) -> tuple[str | None, str | None, list[dict]]:
    """Enactment date, latest CRS summary, and recorded final-passage votes."""
    path = f"/bill/{bill['congress']}/{bill['type'].lower()}/{bill['number']}"
    enacted, votes = None, []
    for action in api.paged(f"{path}/actions", "actions"):
        text = action.get("text", "")
        if text.startswith("Became Public Law"):
            enacted = action.get("actionDate")
        for rv in action.get("recordedVotes") or []:
            if not is_final_passage(text):
                continue
            counts = counts_from_text(text)
            if not counts and rv.get("url"):
                resp = get(client, rv["url"])
                counts = counts_from_xml(resp.text, rv.get("chamber", "")) if resp.is_success else None
            if counts:
                votes.append({"chamber": rv.get("chamber"), "date": rv.get("date", "")[:10],
                              "roll": rv.get("rollNumber"), "yeas": counts[0], "nays": counts[1],
                              "action": text[:300], "url": rv.get("url")})
    # The API lists actions newest first and repeats some votes; keep one per roll call.
    seen, unique = set(), []
    for v in votes:
        k = (v["chamber"], v["date"], v["roll"])
        if k not in seen:
            seen.add(k)
            unique.append(v)
    summaries = api.get(f"{path}/summaries").get("summaries", [])
    summary = _strip_html(summaries[-1]["text"]) if summaries else None
    return enacted, summary, unique


def run(conn, r2=None, since_congress: int | None = None, fetch_text: bool = True,
        max_minutes: float = 100) -> int:
    """Stops cleanly after max_minutes so a backfill fits inside one Actions job;
    the next run picks up where this one stopped."""
    key = require("DATA_GOV_API_KEY")
    deadline = time.monotonic() + max_minutes * 60
    cursor = db.get_cursor(conn, SOURCE) or {}
    latest = current_congress(dt.date.today().year)
    start = since_congress or cursor.get("next_congress") or FIRST_CONGRESS
    count = skipped = 0
    # The cursor never moves past a Congress with skipped laws, so the next run retries them.
    first_incomplete = None
    with http.client() as client:
        api = CongressAPI(client, key)
        for congress in range(start, latest + 1):
            for bill in api.paged(f"/law/{congress}/pub", "bills"):
                law_number = next((law["number"] for law in bill.get("laws", [])
                                   if law.get("type") == "Public Law"), None)
                if not law_number:
                    continue
                if time.monotonic() > deadline:
                    print(f"Time budget reached in Congress {congress}; resuming next run", flush=True)
                    return count
                aid = ids.public_law(*law_number.split("-"))
                row = conn.execute(
                    "SELECT metadata ? 'votes', text_key IS NOT NULL OR metadata ? 'text_missing'"
                    " FROM artifact WHERE id = %s", (aid,)).fetchone()
                if row and row[0] and (row[1] or not fetch_text):
                    continue  # already complete
                try:
                    enacted, summary, votes = law_details(api, client, bill)
                except (RuntimeError, httpx.HTTPError) as err:
                    # One bad record shouldn't stop the backfill; it is retried next run.
                    print(f"Skipping {law_number} for now: {str(err)[:200]}", flush=True)
                    skipped += 1
                    first_incomplete = first_incomplete or congress
                    continue
                artifact = to_artifact(bill, law_number, enacted, summary, votes)
                if fetch_text and r2:
                    c, n = law_number.split("-")
                    resp = get(client, GOVINFO_TEXT.format(c=c, n=n))
                    if resp.is_success:
                        key_ = ids.text_key(artifact["id"])
                        artifact["text_key"] = key_
                        artifact["text_sha256"] = r2.put_text(key_, _strip_html(resp.text))
                    else:
                        artifact["metadata"]["text_missing"] = True
                db.upsert_artifact(conn, artifact)
                conn.commit()
                count += 1
            # Re-scan the current Congress every run so new laws are picked up.
            db.set_cursor(conn, SOURCE,
                          {"next_congress": first_incomplete or min(congress + 1, latest)})
            conn.commit()
            print(f"Congress {congress}: done ({count} laws so far, {skipped} skipped)", flush=True)
    return count

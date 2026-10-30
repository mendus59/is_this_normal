"""Presidential documents (executive orders, proclamations, memoranda, ...) from the
Federal Register API. No API key needed. https://www.federalregister.gov/developers

Queries one year at a time because the API stops paginating past 10,000 results.
"""

import datetime as dt
import re
from collections.abc import Iterator

import httpx

from itn import db, ids
from itn.config import SCOPE_START

API = "https://www.federalregister.gov/api/v1/documents.json"
SOURCE = "federal_register"
FIELDS = [
    "document_number", "title", "type", "subtype", "executive_order_number",
    "signing_date", "publication_date", "html_url", "pdf_url", "raw_text_url", "abstract",
    "citation", "president", "disposition_notes",
]


def _slug(value: str | None) -> str | None:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") if value else None


def fetch_year(client: httpx.Client, year: int) -> Iterator[dict]:
    params: list[tuple[str, str | int]] = [
        ("per_page", 1000),
        ("order", "oldest"),
        ("conditions[type][]", "PRESDOCU"),
        ("conditions[signing_date][gte]", f"{year}-01-01"),
        ("conditions[signing_date][lte]", f"{year}-12-31"),
    ] + [("fields[]", f) for f in FIELDS]
    url: str | None = API
    while url:
        resp = client.get(url, params=params if url == API else None)
        if resp.is_error:
            raise RuntimeError(
                f"Federal Register API returned {resp.status_code} for {year}: {resp.text[:1000]}"
            )
        page = resp.json()
        yield from page.get("results", [])
        url = page.get("next_page_url")


def to_artifact(doc: dict) -> dict:
    """Map one Federal Register result to an artifact row."""
    subtype = _slug(doc.get("subtype")) or "other"
    eo_number = doc.get("executive_order_number")
    artifact_id = (
        ids.executive_order(eo_number)
        if subtype == "executive_order" and eo_number
        else ids.presidential_document(doc["document_number"])
    )
    effective = doc.get("signing_date") or doc.get("publication_date")
    sources = [
        {"label": "Federal Register", "url": doc["html_url"]},
        *([{"label": "PDF", "url": doc["pdf_url"]}] if doc.get("pdf_url") else []),
    ]
    president = doc.get("president") or {}
    return {
        "id": artifact_id,
        "type": "executive_document",
        "subtype": subtype,
        "title": doc["title"],
        "effective_date": effective,
        "in_scope": bool(effective and effective >= SCOPE_START),
        "closure_depth": 0,
        "sources": sources,
        "official_summary": doc.get("abstract"),
        "text_key": None,
        "text_sha256": None,
        "metadata": {
            "fr_document_number": doc["document_number"],
            "fr_citation": doc.get("citation"),
            "executive_order_number": eo_number,
            "president": president.get("name") if isinstance(president, dict) else president,
            "disposition_notes": doc.get("disposition_notes"),
            "raw_text_url": doc.get("raw_text_url"),
        },
    }


def run(conn, r2=None, since_year: int | None = None, fetch_text: bool = True) -> int:
    """Ingest from since_year (or the stored cursor, or 2000) through the current year."""
    cursor = db.get_cursor(conn, SOURCE) or {}
    start = since_year or cursor.get("next_year") or int(SCOPE_START[:4])
    this_year = dt.date.today().year
    count = 0
    with httpx.Client(timeout=60, headers={"User-Agent": "IsThisNormal pipeline"}) as client:
        for year in range(start, this_year + 1):
            for doc in fetch_year(client, year):
                artifact = to_artifact(doc)
                raw_url = artifact["metadata"].get("raw_text_url")
                if fetch_text and r2 and raw_url:
                    text = client.get(raw_url).raise_for_status().text
                    key = ids.text_key(artifact["id"])
                    artifact["text_key"] = key
                    artifact["text_sha256"] = r2.put_text(key, text)
                db.upsert_artifact(conn, artifact)
                count += 1
            # Re-scan the current year every run so late publications are picked up.
            db.set_cursor(conn, SOURCE, {"next_year": min(year + 1, this_year)})
            conn.commit()
    return count

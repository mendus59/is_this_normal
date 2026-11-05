"""U.S. Code sections cited by the corpus, so readers can follow a statute citation to
its text. Text comes from GovInfo's annual editions of the Code through its free link
service (https://www.govinfo.gov/link-docs/), which needs no key.

The text is the latest annual edition, not the version in force when a ruling cited
it; the reader says which edition it shows. Sections are out of scope for scoring
(closure_depth 1).
"""

import html
import re
import time

from itn import db, http, ids
from itn.http import get

SOURCE = "cited_usc"
LINK = "https://www.govinfo.gov/link/uscode/{title}/{section}"
DELAY_SECONDS = 1.0
_TAG = re.compile(r"<[^>]+>")
_EDITION = re.compile(r"USCODE-(\d{4})-title")


def wanted(conn) -> list[tuple[str, str, str]]:
    """(artifact id, title, section) for sections cited by the corpus but not loaded."""
    rows = conn.execute(
        "SELECT DISTINCT c.to_artifact FROM citation c"
        " JOIN component f ON f.id = c.from_component"
        " JOIN artifact src ON src.id = f.artifact_id AND src.closure_depth = 0"
        " LEFT JOIN artifact a ON a.id = c.to_artifact"
        " WHERE c.to_artifact LIKE %s AND a.id IS NULL", ("usc/%",)).fetchall()
    conn.commit()
    out = []
    for (aid,) in rows:
        parts = aid.split("/")
        if len(parts) == 3 and parts[1].isdigit() and "@" not in parts[2]:
            out.append((aid, parts[1], parts[2]))
    return sorted(out, key=lambda r: (int(r[1]), r[2]))


def page_text(page: str) -> str:
    """Readable text from a GovInfo section page."""
    body = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", page)
    body = re.sub(r"(?i)<(br|/p|/div|/h\d|/li|/tr)[^>]*>", "\n", body)
    text = html.unescape(_TAG.sub("", body))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def heading(text: str, title: str, section: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith(f"§{section}.") or line.strip().startswith(f"§ {section}."):
            return f"{title} U.S.C. {line.strip()}"[:300]
    return f"{title} U.S.C. § {section}"


def to_artifact(aid: str, title: str, section: str, text: str, url: str) -> dict:
    edition = _EDITION.search(url)
    return {
        "id": aid,
        "type": "usc_section",
        "subtype": None,
        "title": heading(text, title, section),
        "effective_date": None,
        "in_scope": False,
        "closure_depth": 1,
        "sources": [
            {"label": "GovInfo (U.S. Code)", "url": url},
            {"label": "Office of the Law Revision Counsel",
             "url": f"https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title{title}-section{section}"},
        ],
        "official_summary": None,
        "text_key": ids.text_key(aid),
        "text_sha256": None,
        "metadata": {"usc_title": title, "usc_section": section,
                     "edition": edition.group(1) if edition else None, "source": "govinfo"},
    }


def run(conn, r2=None, fetch_text: bool = True, max_minutes: float = 170, **_) -> int:
    deadline = time.monotonic() + max_minutes * 60
    cursor = db.get_cursor(conn, SOURCE) or {}
    missing = set(cursor.get("not_found", []))
    todo = [r for r in wanted(conn) if r[0] not in missing]
    print(f"Cited U.S. Code sections not yet loaded: {len(todo)} "
          f"({len(missing)} known not to resolve)", flush=True)
    count = 0
    with http.client(timeout=120) as client:
        for aid, title, section in todo:
            if time.monotonic() > deadline:
                print(f"Time budget reached at {aid}; resuming next run", flush=True)
                break
            time.sleep(DELAY_SECONDS)
            try:
                resp = get(client, LINK.format(title=title, section=section),
                           params={"link-type": "html"})
            except Exception as err:  # noqa: BLE001 - one bad fetch is retried next run
                print(f"{aid}: {str(err)[:200]}; retrying next run", flush=True)
                continue
            if resp.status_code in (400, 404):
                missing.add(aid)
                continue
            if resp.is_error:
                print(f"{aid}: {resp.status_code}; retrying next run", flush=True)
                continue
            text = page_text(resp.text)
            if len(text) < 40:
                missing.add(aid)
                continue
            artifact = to_artifact(aid, title, section, text, str(resp.url))
            if fetch_text and r2:
                artifact["text_sha256"] = r2.put_text(artifact["text_key"], text)
            else:
                artifact["text_key"] = None
            db.upsert_artifact(conn, artifact)
            conn.commit()
            count += 1
            if count % 100 == 0:
                db.set_cursor(conn, SOURCE, {"not_found": sorted(missing)})
                conn.commit()
                print(f"{count} sections loaded", flush=True)
    db.set_cursor(conn, SOURCE, {"not_found": sorted(missing)})
    conn.commit()
    print(f"Done: {count} sections loaded, {len(missing)} not found", flush=True)
    return count

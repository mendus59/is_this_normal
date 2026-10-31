"""Supreme Court opinions from the CourtListener API, with every opinion in a case
(majority, concurrences, dissents) stored separately and its citations extracted.
https://www.courtlistener.com/help/api/rest/ (token in env COURTLISTENER_TOKEN)

CourtListener allows 5,000 requests an hour, so calls are paced and each case is
committed as soon as it is complete. A run that stops part way resumes from the
filing date it reached, and cases already loaded are skipped.
"""

import logging
import re
import time

import httpx

from itn import db, http, ids
from itn.config import SCOPE_START, require
from itn.extract import citations
from itn.http import get

API = "https://www.courtlistener.com/api/rest/v4"
SITE = "https://www.courtlistener.com"
SOURCE = "courtlistener_scotus"
# Stay under 5,000 requests per hour.
API_DELAY_SECONDS = 0.8
# Waits longer than this mean the hourly quota is spent; stop and resume next run.
MAX_RATE_WAIT_SECONDS = 180

# eyecite logs a warning for every ambiguous short citation; they are harmless here.
logging.getLogger("eyecite").setLevel(logging.ERROR)


class RateLimited(Exception):
    pass
OPINION_FIELDS = ["id", "type", "author_str", "joined_by_str", "per_curiam", "ordering_key",
                  "plain_text", "html_with_citations", "html_lawbox", "html_columbia",
                  "xml_harvard", "html"]

# CourtListener opinion type codes.
KINDS = {
    "010combined": "opinion", "015unamimous": "unanimous", "020lead": "majority",
    "025plurality": "plurality", "030concurrence": "concurrence",
    "035concurrenceinpart": "concurrence_in_part", "040dissent": "dissent",
    "050addendum": "addendum", "060remittitur": "remittitur", "070rehearing": "rehearing",
    "080onthemerits": "on_the_merits", "090onmotiontostrike": "on_motion_to_strike",
}


def _strip_html(html: str) -> str:
    text = re.sub(r"<(br|/p|/div|/h\d)[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", text)))
    text = re.sub(r"&#?\w+;", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def opinion_text(op: dict) -> str:
    if (op.get("plain_text") or "").strip():
        return op["plain_text"].strip()
    for field in ("html_with_citations", "html_lawbox", "html_columbia", "xml_harvard", "html"):
        if (op.get(field) or "").strip():
            return _strip_html(op[field])
    return ""


def us_cite(cluster: dict) -> tuple[str, str] | None:
    for c in cluster.get("citations") or []:
        if c.get("reporter") == "U.S." and str(c.get("volume", "")).isdigit() \
                and str(c.get("page", "")).isdigit():
            return str(c["volume"]), str(c["page"])
    return None


_CITE = re.compile(r"^\s*(\d+)\s+(.+?)\s+(\d+)\s*$")


def cluster_from_hit(hit: dict) -> dict:
    """Case listing rows come from the search API, which is indexed and fast; filtering
    the clusters endpoint by court times out. Reshape a hit like a cluster."""
    cites = []
    for c in hit.get("citation") or []:
        m = _CITE.match(c)
        if m:
            cites.append({"volume": m.group(1), "reporter": m.group(2), "page": m.group(3)})
    return {
        "id": hit["cluster_id"],
        "absolute_url": hit.get("absolute_url") or f"/opinion/{hit['cluster_id']}/",
        "case_name": hit.get("caseName"),
        "case_name_full": hit.get("caseNameFull"),
        "date_filed": (hit.get("dateFiled") or "")[:10] or None,
        "citations": cites,
        "syllabus": hit.get("syllabus"),
        "judges": hit.get("judge"),
        "docket_number": hit.get("docketNumber"),
        "status": hit.get("status"),
        "scdb_id": hit.get("scdb_id"),
    }


def artifact_id(cluster: dict, docket_number: str | None) -> str | None:
    cite = us_cite(cluster)
    if cite:
        return ids.scotus_case(*cite)
    return ids.scotus_docket(docket_number) if docket_number else None


def split_names(value: str | None) -> list[str]:
    return [n.strip() for n in re.split(r",|;| and ", value or "") if n.strip()]


def to_artifact(cluster: dict, aid: str, docket_number: str | None) -> dict:
    date = cluster.get("date_filed")
    summary = _strip_html(cluster.get("syllabus") or cluster.get("summary") or "") or None
    cite = us_cite(cluster)
    return {
        "id": aid,
        "type": "scotus_case",
        "subtype": "opinion",
        "title": cluster.get("case_name") or cluster.get("case_name_full") or aid,
        "effective_date": date,
        "in_scope": bool(date and date >= SCOPE_START),
        "closure_depth": 0,
        "sources": [{"label": "CourtListener", "url": SITE + cluster["absolute_url"]}],
        "official_summary": summary[:4000] if summary else None,
        "text_key": None,
        "text_sha256": None,
        "metadata": {
            "courtlistener_cluster": cluster["id"],
            "us_cite": f"{cite[0]} U.S. {cite[1]}" if cite else None,
            "docket_number": docket_number,
            "judges": cluster.get("judges"),
            "scdb_id": cluster.get("scdb_id"),
        },
    }


def to_components(aid: str, opinions: list[dict]) -> list[tuple[dict, str]]:
    """(component row, text) for each opinion, in the order they appear in the case."""
    ordered = sorted(opinions, key=lambda o: (o.get("ordering_key") is None,
                                              o.get("ordering_key") or 0, o["id"]))
    out = []
    for i, op in enumerate(ordered):
        kind = KINDS.get(op.get("type") or "", "other")
        author = (op.get("author_str") or "").strip() or ("Per Curiam" if op.get("per_curiam") else None)
        cid = ids.component(aid, i, kind)
        out.append(({"id": cid, "artifact_id": aid, "kind": kind, "author": author,
                     "joined_by": split_names(op.get("joined_by_str")), "ord": i,
                     "text_key": None}, opinion_text(op)))
    return out


class CourtListener:
    def __init__(self, client, token: str):
        self.client = client
        client.headers["Authorization"] = f"Token {token}"

    def get(self, url: str, params=None) -> dict:
        time.sleep(API_DELAY_SECONDS)
        url = url if url.startswith("http") else f"{API}{url}"
        resp = get(self.client, url, params=params, max_wait=MAX_RATE_WAIT_SECONDS)
        if resp.status_code == 429:
            raise RateLimited(f"Retry-After {resp.headers.get('Retry-After', '?')}s")
        if resp.is_error:
            raise RuntimeError(f"CourtListener returned {resp.status_code} for {url}: {resp.text[:500]}")
        return resp.json()

    def paged(self, path: str, params: dict):
        page = self.get(path, params)
        while True:
            yield from page.get("results", [])
            if not page.get("next"):
                return
            page = self.get(page["next"])


def run(conn, r2=None, since: str | None = None, fetch_text: bool = True,
        max_minutes: float = 95) -> int:
    """Stops cleanly after max_minutes; the next run resumes from the filing date reached."""
    token = require("COURTLISTENER_TOKEN")
    deadline = time.monotonic() + max_minutes * 60
    cursor = db.get_cursor(conn, SOURCE) or {}
    start = since or cursor.get("next_date") or SCOPE_START
    count = skipped = 0
    with http.client() as client:
        cl = CourtListener(client, token)
        params = {"type": "o", "court": "scotus", "filed_after": start,
                  "order_by": "dateFiled asc"}
        try:
            for hit in cl.paged("/search/", params):
                cluster = cluster_from_hit(hit)
                if cluster["status"] not in (None, "Published"):
                    continue
                if time.monotonic() > deadline:
                    print(f"Time budget reached at {cluster.get('date_filed')}; resuming next run", flush=True)
                    break
                done = conn.execute(
                    "SELECT 1 FROM artifact WHERE metadata->>'courtlistener_cluster' = %s"
                    " AND metadata ? 'opinions'", (str(cluster["id"]),)).fetchone()
                # Don't hold a transaction open across network waits.
                conn.commit()
                if not done:
                    try:
                        docket_number = cluster["docket_number"]
                        aid = artifact_id(cluster, docket_number)
                        opinions = list(cl.paged("/opinions/", {"cluster": cluster["id"],
                                                                "fields": ",".join(OPINION_FIELDS)}))
                    except (RuntimeError, httpx.HTTPError) as err:
                        # One bad record shouldn't stop the backfill; it is retried next run.
                        print(f"Skipping cluster {cluster['id']} for now: {str(err)[:200]}", flush=True)
                        skipped += 1
                        continue
                    if not aid:
                        print(f"Skipping cluster {cluster['id']}: no U.S. cite or docket number", flush=True)
                        continue
                    artifact = to_artifact(cluster, aid, docket_number)
                    comps = to_components(aid, opinions)
                    artifact["metadata"]["opinions"] = len(comps)
                    db.upsert_artifact(conn, artifact)
                    for comp, text in comps:
                        if text and fetch_text and r2:
                            comp["text_key"] = ids.component_text_key(comp["id"])
                            r2.put_text(comp["text_key"], text)
                        db.upsert_component(conn, comp)
                        db.replace_citations(conn, comp["id"], citations.EXTRACTOR,
                                             citations.extract(text, artifact["effective_date"]))
                    conn.commit()
                    count += 1
                # Only move the cursor while nothing has been skipped, so skips are retried.
                if not skipped and cluster.get("date_filed"):
                    db.set_cursor(conn, SOURCE, {"next_date": cluster["date_filed"]})
                    conn.commit()
                if count and count % 100 == 0 and not done:
                    print(f"{cluster.get('date_filed')}: {count} cases so far, {skipped} skipped", flush=True)
        except RateLimited as err:
            print(f"CourtListener rate limit reached ({err}); resuming next run", flush=True)
    print(f"Done: {count} cases, {skipped} skipped", flush=True)
    return count

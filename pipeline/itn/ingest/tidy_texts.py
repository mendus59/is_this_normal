"""Free: bring texts stored before 2026-11-25 up to how they are stored now
(`itn ingest tidy-texts`). Safe to re-run; each step skips what is already right.

- Laws: older loads flattened GovInfo's text onto one line, losing its sections and
  lettered items. Those are fetched again and stored with their lines (law_text).
- Rulings from the Court's PDFs: put back the fi/fl ligatures the PDFs drop ("offcer").
- Official summaries: decode HTML entities left in CRS summaries (&nbsp;, &quot;).
"""

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor

from itn import http, ligatures
from itn.http import get
from itn.ingest import congress_laws

WORKERS = 8
GOVINFO_WORKERS = 3
_LAW_ID = re.compile(r"^pl/(\d+)-(\d+)$")
_ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]*);", re.I)


def clean_summary(text: str) -> str:
    return re.sub(r"[\s ]+", " ", html.unescape(text)).strip()


def summaries(conn, log=print) -> int:
    rows = conn.execute("SELECT id, official_summary FROM artifact WHERE official_summary ~ '&[#a-zA-Z0-9]+;'"
                        ).fetchall()
    n = 0
    for aid, text in rows:
        clean = clean_summary(text)
        if clean != text:
            conn.execute("UPDATE artifact SET official_summary = %s WHERE id = %s", (clean, aid))
            n += 1
    conn.commit()
    log(f"Summaries: {n} of {len(rows)} with HTML entities decoded")
    return n


def laws(conn, r2, deadline: float, log=print) -> int:
    rows = conn.execute("SELECT id, text_key FROM artifact WHERE type = 'public_law' AND text_key IS NOT NULL"
                        " ORDER BY id").fetchall()
    conn.commit()

    def flat(row):
        text = r2.get_text(row[1]) or ""
        return row if text.strip() and "\n" not in text.strip() else None

    with ThreadPoolExecutor(WORKERS) as pool:
        todo = [r for r in pool.map(flat, rows) if r]
    log(f"Laws: {len(todo)} of {len(rows)} stored on one line")
    done = failed = 0
    with http.client() as client:
        def refetch(row):
            aid, key = row
            if time.monotonic() > deadline:
                return aid, None
            c, n = _LAW_ID.match(aid).groups()
            resp = get(client, congress_laws.GOVINFO_TEXT.format(c=c, n=n))
            if not resp.is_success or "<pre" not in resp.text.lower():
                return aid, False
            return aid, r2.put_text(key, congress_laws.law_text(resp.text))

        with ThreadPoolExecutor(GOVINFO_WORKERS) as pool:
            for aid, digest in pool.map(refetch, [r for r in todo if _LAW_ID.match(r[0])]):
                if digest is None:
                    continue
                if digest is False:
                    failed += 1
                    log(f"  {aid}: GovInfo text not available; left as it was")
                    continue
                conn.execute("UPDATE artifact SET text_sha256 = %s WHERE id = %s", (digest, aid))
                conn.commit()
                done += 1
                if done % 200 == 0:
                    log(f"  {done} laws re-stored")
    left = len(todo) - done - failed
    log(f"Laws: {done} re-stored with their lines, {failed} unavailable"
        + (f", {left} left for the next run (time budget)" if left else ""))
    return done


def rulings(conn, r2, deadline: float, log=print) -> int:
    keys = [k for (k,) in conn.execute(
        "SELECT c.text_key FROM component c JOIN artifact a ON a.id = c.artifact_id"
        " WHERE a.type = 'scotus_case' AND c.text_key IS NOT NULL ORDER BY c.text_key").fetchall()]
    conn.commit()

    def repair(key):
        if time.monotonic() > deadline:
            return None
        text = r2.get_text(key) or ""
        fixed = ligatures.fix(text)
        if fixed == text:
            return False
        r2.put_text(key, fixed)
        return True

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(repair, keys))
    fixed = sum(1 for r in results if r)
    skipped = sum(1 for r in results if r is None)
    log(f"Rulings: {fixed} of {len(keys)} opinion texts had lost ligatures put back"
        + (f"; {skipped} left for the next run (time budget)" if skipped else ""))
    return fixed


def run(conn, r2, max_minutes: float = 95, log=print) -> int:
    deadline = time.monotonic() + max_minutes * 60
    return summaries(conn, log) + laws(conn, r2, deadline, log) + rulings(conn, r2, deadline, log)

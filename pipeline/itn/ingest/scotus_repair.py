"""Repairs to Supreme Court rulings already loaded, from material already stored.

Two things the loaders got wrong for some cases:

* Denials of certiorari printed with a long statement or dissent were loaded as rulings.
  The Court decided nothing in them, so they are taken out of scope.
* Rulings cut from the bound volumes are titled "No. 19-5421" when Oyez has no name for
  the docket (it lists argued cases, and only some terms were asked for). Names come from
  the Court's own slip opinion lists and Oyez, then from the caption printed on the page.
"""

import re

from itn import db, http
from itn.http import get
from itn.ingest import supremecourt_gov as sc

SOURCE = "scotus_repair"
OYEZ = "https://api.oyez.org"
HEAD_CHARS = 600

# The entry opens with the order ("Ct. App. Ohio, Lucas County. Certiorari denied.") or with
# a Justice's statement or dissent about it. "cert. denied" inside a citation does not match.
_DENIAL = re.compile(
    r"\bCertiorari denied\."
    r"|\b(?:dissenting from|respecting|concurring in)\s+(?:the\s+)?denial\s+of\s+"
    r"(?:the\s+petitions?\s+for\s+(?:an?\s+)?writs?\s+of\s+)?certiorari\b")


def is_denial(text: str | None) -> bool:
    """True when the text opens as a denial of certiorari, not a ruling."""
    return bool(_DENIAL.search((text or "")[:HEAD_CHARS]))


def drop_denials(conn, r2, log=print) -> int:
    """Take out of scope every in-scope ruling whose first stored opinion opens as a
    denial of certiorari. Reversible: the artifact stays, marked as an order."""
    rows = conn.execute(
        "SELECT DISTINCT ON (a.id) a.id, a.title, c.text_key FROM artifact a"
        " JOIN component c ON c.artifact_id = a.id AND c.text_key IS NOT NULL"
        " WHERE a.type = 'scotus_case' AND a.in_scope ORDER BY a.id, c.ord").fetchall()
    conn.commit()
    gone = []
    for aid, title, key in rows:
        head = (r2.get_text(key) or "")[:HEAD_CHARS]
        if is_denial(head):
            gone.append(aid)
            log(f"  denial, not a ruling: {aid} {title}: {' '.join(head.split())[:160]}")
    if gone:
        conn.execute("UPDATE artifact SET in_scope = false, metadata = metadata || '{\"order\": true}'"
                     " WHERE id = ANY(%s)", (gone,))
        conn.commit()
    log(f"Checked {len(rows)} rulings; {len(gone)} denials of certiorari taken out of scope")
    return len(gone)


_UNNAMED = r"^Nos?\. "
_CAPTION_END = re.compile(r"^(?:certiorari|appeals?|on |No\.|Nos\.)")
_CAPTION_SKIP = re.compile(r"OCTOBER\s+TERM|^Syl|^labus$|^Syllabus$|^Per Curiam|IN CHAMBERS|INDIVIDUAL JUSTICE"
                           r"|CASES ADJUDGED|^IN THE$|^AT$|SUPREME COURT OF THE UNITED STATES|^\d+$")
_PARTIES = re.compile(r"^(?:[A-Z][A-Za-z0-9 .,'’&()-]* v\. [A-Z][A-Za-z0-9 .,'’&()-]*|(?:IN RE|EX PARTE) [A-Z].*)$")
_SMALL = {"of", "the", "and", "for", "in", "on", "to", "a", "an", "at", "by", "ex", "rel", "re"}
_KEEP = {"USA", "LLC", "LLP", "FCC", "EPA", "SEC", "FDA", "FTC", "NLRB", "IRS", "FBI", "CIA", "INS", "II", "III", "IV"}


def _title_case(name: str) -> str:
    words = []
    for k, w in enumerate(name.split()):
        core = w.strip(".,")
        if w in ("v.", "et", "al.", "al.,"):
            words.append(w)
        elif core in _KEEP or not core.isupper():
            words.append(w)
        elif k and core.lower() in _SMALL:
            words.append(w.lower())
        else:
            words.append("-".join(p.capitalize() for p in w.split("-")))
    return " ".join(words)


def _party(side: str) -> str:
    """A party as case names print it: without "et al." and without the office or
    description that follows a comma ("BARR, ATTORNEY GENERAL, et al." -> "BARR")."""
    side = re.sub(r",?\s+et al\.?,?$", "", side.strip())
    first, _, rest = side.partition(",")
    if re.match(r"\s*(?:INC|LLC|L\. ?P|L\. ?L\. ?C|LTD|CORP|CO|N\. ?A)\b", rest):
        return side.split(",", 2)[0] + "," + side.split(",", 2)[1]
    return first.strip()


def caption_name(text: str | None) -> str | None:
    """The case name from the caption at the top of a bound-volume page, or None when the
    caption is missing or incomplete (the PDF text often loses part of it)."""
    lines = []
    for raw in (text or "").splitlines()[:14]:
        line = " ".join(raw.split())
        if not line or _CAPTION_SKIP.search(line):
            continue
        if _CAPTION_END.match(line):
            break
        lines.append(line)
    name = " ".join(lines)
    if not name or len(name) > 200 or not _PARTIES.match(name):
        return None
    if " v. " in name:
        left, right = name.split(" v. ", 1)
        if len(left) < 3 or len(right) < 3:
            return None
        name = f"{_party(left)} v. {_party(right)}"
    else:
        name = re.sub(r",?\s+et al\.?$", "", name)
    return _title_case(name)


def clean_docket(docket: str | None) -> str:
    return re.sub(r"^Nos?\.?\s*", "", (docket or "").strip()).replace("–", "-")


def slip_names(client, terms: list[int], log=print) -> dict[str, str]:
    """Docket number -> case name from the Court's slip opinion lists, which it keeps from
    October Term 2017 and which include the unargued per curiams."""
    names: dict[str, str] = {}
    for term in terms:
        if term < 2017:
            continue
        try:
            resp = get(client, f"{sc.SITE}/opinions/slipopinion/{term % 100:02d}")
            if resp.is_error:
                continue
            for row in sc.parse_term_table(resp.text, str(resp.url)):
                if row.get("name") and row["name"] != row["docket"]:
                    names[clean_docket(row["docket"])] = row["name"]
        except Exception as err:  # noqa: BLE001 - one list failing leaves the others
            log(f"Slip opinion list {term} unavailable: {str(err)[:200]}")
    return names


def listed_names(client, terms: list[int], log=print) -> dict[str, str]:
    """Docket number -> case name, from Oyez (argued cases) and then the Court's own lists."""
    names: dict[str, str] = {}
    for term in terms:
        try:
            resp = get(client, f"{OYEZ}/cases", params={"per_page": 0, "filter": f"term:{term}"})
            resp.raise_for_status()
            for case in resp.json():
                if case.get("docket_number") and case.get("name"):
                    names[clean_docket(case["docket_number"])] = case["name"]
        except Exception as err:  # noqa: BLE001 - one list failing leaves the others
            log(f"Oyez term {term} unavailable: {str(err)[:200]}")
    names.update(slip_names(client, terms, log))
    return names


def retitle(conn, r2, client, tried: set[str], log=print) -> int:
    """Give rulings titled by docket number their case name."""
    rows = conn.execute(
        "SELECT a.id, a.title, a.effective_date, a.metadata->>'docket_number', a.official_summary,"
        " (SELECT c.text_key FROM component c WHERE c.artifact_id = a.id AND c.text_key IS NOT NULL"
        "  ORDER BY c.ord LIMIT 1) FROM artifact a"
        " WHERE a.type = 'scotus_case' AND a.title ~ %s ORDER BY a.id", (_UNNAMED,)).fetchall()
    conn.commit()
    rows = [r for r in rows if r[0] not in tried]
    if not rows:
        return 0
    terms = sorted({sc.term_of(r[2]) for r in rows if r[2]})
    names = listed_names(client, terms, log)
    log(f"{len(rows)} rulings titled by docket number; {len(names)} names listed for terms {terms}")
    fixed = 0
    for aid, title, _, docket, summary, key in rows:
        docket = clean_docket(docket or title)
        name = names.get(docket) or caption_name(summary)
        if not name and key and r2:
            name = caption_name((r2.get_text(key) or "")[:2000])
        tried.add(aid)
        if not name:
            log(f"  no name found: {aid} {title}")
            continue
        conn.execute("UPDATE artifact SET title = %s WHERE id = %s", (name, aid))
        log(f"  {aid}: {title} -> {name}")
        fixed += 1
    conn.commit()
    return fixed


def run(conn, r2=None, **_) -> int:
    cursor = db.get_cursor(conn, SOURCE) or {}
    changed = 0
    if r2 and not cursor.get("denials_dropped_v1"):
        changed += drop_denials(conn, r2)
        cursor["denials_dropped_v1"] = True
    tried = set(cursor.get("untitled_tried_v1", []))
    with http.client(timeout=120) as client:
        changed += retitle(conn, r2, client, tried)
    cursor["untitled_tried_v1"] = sorted(tried)
    db.set_cursor(conn, SOURCE, cursor)
    conn.commit()
    return changed

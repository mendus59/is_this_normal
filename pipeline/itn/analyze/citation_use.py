"""Paid: how each citation is used. For every citation of a Supreme Court case, a U.S. Code
section or a public law in a ruling's opinions (majority, concurrences and dissents) or an
executive document's text, a model judges whether the citing passage uses the authority
appropriately, as a stretch (not technically wrong, but beyond what it holds), or
improperly. "Improper" is published only when a second, independent check agrees.

The model sees, for each citation: the passage around it (from the citing opinion's own
text), the cited authority's free official summary or syllabus, and the part of the cited
text that best matches the passage (where a quotation in the passage appears, when it
does). Every cited authority is dated before the citing item took effect: the filter is in
the SQL below, not only in the prompt. U.S. Code sections are undated and come from the
latest annual edition, which the prompt says may differ from the text in force then.

Citations are sent in groups of up to CHUNK per opinion, so the instructions and each
group's authorities are paid for once per group. Results are cached by input like every
other stage, and stored per citation in `citation_use`.
"""

import math
import random
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from itn import db
from itn.analyze import data
from itn.analyze.estimate import RANGE, _keys, cost

STAGE = "citation_use"
VERDICTS = ["appropriate", "stretch", "improper", "insufficient"]
TYPES = ("scotus_case", "executive_document")      # laws' citations are the sections they amend
SKIP_KINDS = ("syllabus",)                          # the Reporter's summary repeats the opinion's cites
CHUNK = 20
BEFORE_CHARS, AFTER_CHARS = 700, 250
SUMMARY_CHARS = 700
EXCERPT_CHARS = 700
CITED_TEXT_MAX = 250_000
TEXT_CACHE = 400
READERS = 16

KIND_LABEL = {"majority": "opinion of the Court", "unanimous": "opinion of the Court", "plurality": "plurality opinion",
              "per_curiam": "per curiam opinion", "concurrence": "concurring opinion", "dissent": "dissenting opinion",
              "concurrence_in_part": "opinion concurring in part and dissenting in part", "text": "document text"}

SYSTEM = """You check how court opinions and presidential documents use the authorities they
cite, for IsThisNormal, a nonpartisan project. For each citation you get the citing passage
(the cited reference is marked with ⟦ ⟧), what the cited authority says (its official
summary or syllabus and the part of its text that best matches the passage), and the kind
of opinion it appears in.

Judge one thing: does the passage use the authority for something the authority actually
says or holds? Pick one verdict:
- appropriate: the authority supports the use, or is fairly described. This includes
  citing a case in order to distinguish, criticize, limit or overrule it, "see also" and
  "cf." citations that point to related support, background citations, and quotations
  that match the cited text.
- stretch: not technically wrong, but beyond what the authority held. For example, dicta
  treated as a holding, a holding extended to a different setting without saying so, a
  quotation that is accurate but loses its qualifying context, a "see" citation whose
  support is indirect, or a statute read more broadly than its words.
- improper: the authority does not say this or says the opposite. For example, the
  holding is misstated, a quotation does not appear or its meaning is altered, the case
  is cited for a rule it rejected, or the cited provision does not address the point.
- insufficient: the material given is not enough to tell. Use this, not stretch, when
  the passage relies on a pinpoint page, a quotation or a specific statement that the
  excerpt and summary do not show: missing material is not evidence of a stretch. Use it
  too when the cited authority's title does not match the case or statute the passage
  names (the corpus may hold a different document at that page).

Rules:
- Judge only from the material given. It contains nothing dated after the citing item.
  Do not use what you know about later events or later cases.
- Disagreeing with the opinion's conclusion is not a misuse. Dissents and concurrences are
  judged by the same standard as majorities; arguing that a case was wrongly decided is
  an appropriate use of it when the case is described accurately.
- A quotation that is not found in the excerpt is not by itself improper or a stretch:
  texts come from different sources and can differ in formatting or be incomplete.
- Stretch and improper need a positive reason from the material: what the authority
  actually held or said, and how the passage goes beyond it or contradicts it.
- U.S. Code text is the latest edition and may differ from the text in force at the time.
  Do not call a citation improper only because the current wording differs.
- Be even-handed across courts, justices, parties and presidents. Describe; do not
  editorialize.
- Give a reason of at most 30 words for each citation, naming what the authority says.

Answer only with the requested JSON, one entry per citation number."""


def schema() -> dict:
    entry = {"type": "object", "properties": {"n": {"type": "integer"},
                                              "verdict": {"type": "string", "enum": VERDICTS},
                                              "reason": {"type": "string"}},
             "required": ["n", "verdict", "reason"], "additionalProperties": False}
    return {"type": "object", "properties": {"checks": {"type": "array", "items": entry}},
            "required": ["checks"], "additionalProperties": False}


# ---- free text work: where each citation sits, and what the cited text says ---------

_WS = re.compile(r"\s+")
_QUOTE = re.compile(r"[“\"]([^”\"]{20,600})[”\"]")
_ELIDE = re.compile(r"\.\s?\.\s?\.|…|\[[^\]]{0,40}\]")
_WORD = re.compile(r"[a-z]{5,}")
_STOP = {"which", "there", "their", "would", "could", "should", "these", "those", "about", "under", "other",
         "state", "states", "court", "courts", "united", "where", "while", "after", "before", "because"}


def norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


def _pattern(raw: str) -> re.Pattern:
    return re.compile(r"\s+".join(re.escape(p) for p in raw.split()))


def locate(text: str, raw: str, pin: str | None, used: set[int]) -> tuple[int, int] | None:
    """Span of this citation in its opinion's text: the first occurrence of its raw text not
    already taken by an earlier citation, preferring one followed by its pinpoint page."""
    if not text or not raw.strip():
        return None
    spans = [m.span() for m in _pattern(raw).finditer(text) if m.start() not in used]
    if not spans:
        return None
    if pin:
        pin_re = re.compile(r"^[^A-Za-z]{0,12}?(?:at\s+)?" + re.escape(str(pin).strip(", ")) + r"\b")
        for s, e in spans:
            if pin_re.search(text[e:e + 40]):
                return s, e
    return spans[0]


def passage(text: str, span: tuple[int, int]) -> str:
    s, e = span
    return norm(f"{text[max(0, s - BEFORE_CHARS):s]}⟦{text[s:e]}⟧{text[e:e + AFTER_CHARS]}")


def _window(text: str, i: int, chars: int, lead: int | None = None) -> str:
    start = max(0, i - (chars // 3 if lead is None else lead))
    if start:
        sp = text.find(" ", start)
        start = sp + 1 if 0 <= sp < start + 40 else start
    return text[start:start + chars].strip()


def excerpt(passage_text: str, cited_text: str, chars: int = EXCERPT_CHARS) -> tuple[str, bool | None]:
    """The part of the cited text that matters for this passage, and whether a quotation in
    the passage was found there (None when the passage quotes nothing checkable)."""
    if not cited_text:
        return "", None
    ct = norm(cited_text)
    low = ct.lower()
    before = passage_text.split("⟦", 1)[0][-500:] + passage_text.split("⟧", 1)[-1][:150]
    pieces = []
    for q in _QUOTE.findall(before):
        pieces += [p.strip(" ,.;:'\"") for p in _ELIDE.split(norm(q).lower())]
    pieces = sorted({p for p in pieces if len(p) >= 20}, key=len, reverse=True)
    for piece in pieces:
        i = low.find(piece[:150])
        if i >= 0:
            return _window(ct, i, chars), True
    found = False if pieces else None
    words = set(_WORD.findall(before.lower())) - _STOP
    if not words:
        return ct[:chars], found
    best, best_i = -1, 0
    step = chars // 2
    for i in range(0, max(1, len(ct) - step), step):
        score = len(words & set(_WORD.findall(low[i:i + chars])))
        if score > best:
            best, best_i = score, i
    return _window(ct, best_i, chars, lead=0), found


# ---- requests ----------------------------------------------------------------------

@dataclass
class Cite:
    citation_id: int
    to: str
    raw: str
    pin: str | None
    title: str
    cited_type: str
    cited_date: str | None
    summary: str
    passage: str
    excerpt: str = ""
    quote_found: bool | None = None


@dataclass
class Group:
    subject: str
    item: object
    component: str
    label: str
    cites: list[Cite] = field(default_factory=list)


def user_prompt(g: Group, cites: list[Cite] | None = None, second: bool = False) -> str:
    cites = cites if cites is not None else g.cites
    it = g.item
    auth: OrderedDict[str, str] = OrderedDict()
    for c in cites:
        auth.setdefault(c.to, f"[{c.to}] {c.title} ({c.cited_type.replace('_', ' ')}"
                              f"{', ' + c.cited_date if c.cited_date else ''})"
                              + (f": {c.summary}" if c.summary else ""))
    lines = [f"Citing item {it.id}: {it.title} ({it.type.replace('_', ' ')}, effective {it.effective_date};"
             f" material as of {it.as_of}).",
             f"Part: {g.label}.", "", "Authorities cited:", *auth.values(), "", "Citations:"]
    for n, c in enumerate(cites, 1):
        q = {True: "a quotation in the passage was found in the cited text; excerpt starts there",
             False: "a quotation in the passage was not found in the cited text available here",
             None: "best-matching part of the cited text"}[c.quote_found]
        lines += [f"#{n} cites [{c.to}] as \"{c.raw}{', ' + c.pin if c.pin else ''}\"",
                  f"Passage: {c.passage}"]
        if c.excerpt:
            lines.append(f"Cited text ({q}): {c.excerpt}")
        lines.append("")
    lines.append("For each citation number, judge how the passage uses the authority.")
    if second:
        lines.append("(Second, independent check of citations a first reviewer called improper."
                     " Do not assume the first reviewer was right.)")
    return "\n".join(lines)


def parse(output: dict | None, n: int) -> dict[int, tuple[str, str]]:
    """Citation number (1-based) -> (verdict, reason); unknown numbers and verdicts dropped."""
    out = {}
    for e in (output or {}).get("checks") or []:
        k, v = e.get("n"), e.get("verdict")
        if isinstance(k, int) and 1 <= k <= n and v in VERDICTS and k not in out:
            out[k] = (v, (e.get("reason") or "").strip()[:400])
    return out


def settle(first: str, second: str | None) -> str:
    """Improper stands only when the second check agrees; otherwise the second check's
    verdict is shown. Without a second check (it failed), the first stands for now and the
    next run re-checks it."""
    if first != "improper" or second is None:
        return first
    return second


# ---- data (every cited authority filtered to before the citing item's date) --------

_ROWS = """
SELECT f.artifact_id, c.id, c.from_component, f.kind, f.author, f.text_key, c.to_artifact, c.pinpoint,
       c.raw_text, c.context_snippet, t.title, t.type, t.effective_date, left(t.official_summary, %s)
FROM citation c
JOIN component f ON f.id = c.from_component
JOIN artifact a ON a.id = f.artifact_id
JOIN artifact t ON t.id = c.to_artifact
WHERE f.artifact_id = ANY(%s) AND f.kind <> ALL(%s) AND c.to_artifact <> f.artifact_id
  AND (t.effective_date IS NULL OR t.effective_date < a.effective_date)
ORDER BY f.artifact_id, f.ord, c.id
"""


def load_rows(conn, ids: list[str]) -> list[tuple]:
    rows = conn.execute(_ROWS, (SUMMARY_CHARS, ids, list(SKIP_KINDS))).fetchall()
    conn.commit()
    return rows


def _with_retry(fn, part):
    from itn.analyze.stages import _with_retry as retry  # stages imports this module lazily
    retry(fn, part)


class TextCache:
    """Recently read R2 texts, shared across reader threads."""

    def __init__(self, r2, size: int = TEXT_CACHE):
        self.r2, self.size, self.lock, self.items = r2, size, threading.Lock(), OrderedDict()

    def get(self, keys: tuple[str, ...], max_chars: int) -> str:
        with self.lock:
            if keys in self.items:
                self.items.move_to_end(keys)
                return self.items[keys]
        text = data.text_of(self.r2, list(keys), max_chars) if keys else ""
        with self.lock:
            self.items[keys] = text
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return text


_CASE_NAME = re.compile(r"\bv\.\s|\bIn re\b|\bEx parte\b")
_NAME_WORD = re.compile(r"[A-Za-z][A-Za-z'&-]{3,}")
_GENERIC = {"united", "states", "america", "city", "county", "state", "commission", "board", "department",
            "company", "corp", "corporation", "inc.", "district", "court", "parte", "people", "secretary",
            "national", "american", "association", "federal", "petitioner", "respondent", "others"}


def name_mismatch(passage_text: str, title: str, cited_type: str) -> bool:
    """True when the passage names a case right before the citation and the cited item's
    title shares no distinctive word with it: the corpus holds a different document at that
    page (an orders-list page, a reporter collision), so the check would judge the wrong case."""
    if cited_type != "scotus_case" or not title:
        return False
    # Only the citation's own clause: in a string cite, an earlier case's name is not this one's.
    before = re.split(r"[;)]", passage_text.split("⟦", 1)[0][-160:])[-1]
    if not _CASE_NAME.search(before):
        return False
    words = {w.lower() for w in _NAME_WORD.findall(title)} - _GENERIC
    if not words:
        return False
    low = before.lower()
    return not any(w in low for w in words)


def groups_for(item, rows: list[tuple], r2, cited_keys: dict[str, list[str]], cache: TextCache,
               skipped: list | None = None) -> list[Group]:
    """One or more groups of up to CHUNK citations per opinion of `item`. Citations whose
    cited page holds a different case are left out (and listed in `skipped`)."""
    by_comp: OrderedDict[str, list[tuple]] = OrderedDict()
    for r in rows:
        by_comp.setdefault(r[2], []).append(r)
    out = []
    for comp, crow in by_comp.items():
        kind, author, key = crow[0][3], crow[0][4], crow[0][5]
        text = (r2.get_text(key) or "") if key else ""
        used: set[int] = set()
        cites = []
        for (_, cid, _, _, _, _, to, pin, raw, ctx, title, typ, date, summary) in crow:
            span = locate(text, raw, pin, used)
            if span:
                used.add(span[0])
                p = passage(text, span)
            else:  # no opinion text: the stored context, with the citation marked when found
                cspan = locate(ctx or "", raw, pin, set())
                p = passage(ctx, cspan) if cspan else norm(ctx or "")
            if not p:
                continue
            if name_mismatch(p, title, typ):
                if skipped is not None:
                    skipped.append(cid)
                continue
            cited = cache.get(tuple(cited_keys.get(to) or []), CITED_TEXT_MAX)
            ex, found = excerpt(p, cited)
            cites.append(Cite(cid, to, raw, pin, title, typ, date.isoformat() if date else None,
                              norm(summary or ""), p, ex, found))
        label = KIND_LABEL.get(kind, kind.replace("_", " ")) + (f" by {author}" if author else "")
        for k in range(0, len(cites), CHUNK):
            out.append(Group(f"{comp}@{k // CHUNK}", item, comp, label, cites[k:k + CHUNK]))
    return out


def build_groups(r2, items: list, log=print) -> tuple[list[Group], list[int]]:
    by_item: dict[str, list[tuple]] = {}
    ids = [i.id for i in items]
    for k in range(0, len(ids), 100):
        part = ids[k:k + 100]
        data_rows = []
        _with_retry(lambda c, p: data_rows.extend(load_rows(c, p)), part)
        for r in data_rows:
            by_item.setdefault(r[0], []).append(r)
    cited = sorted({r[6] for rows in by_item.values() for r in rows})
    keys: dict[str, list[str]] = {}
    for k in range(0, len(cited), 500):
        _with_retry(lambda c, p: keys.update(_keys(c, p)), cited[k:k + 500])
    cache = TextCache(r2)
    log(f"{STAGE}: {sum(len(v) for v in by_item.values())} citations in {len(by_item)} items"
        f" ({len(cited)} distinct authorities)")
    todo = [i for i in items if i.id in by_item]
    with ThreadPoolExecutor(READERS) as pool:
        skipped: list[int] = []
        parts = list(pool.map(lambda i: groups_for(i, by_item[i.id], r2, keys, cache, skipped), todo))
    if skipped:
        log(f"{STAGE}: {len(skipped)} citations left out: the cited page holds a different case")
    return [g for part in parts for g in part], skipped


# ---- the run -----------------------------------------------------------------------

def run(p, ids: list[str]) -> dict:
    """Check every citation of `ids` with the pipeline `p` (cache, batches, budget)."""
    with db.connect() as c:
        items = data.load_items(c, ids)
    groups, skipped = build_groups(p.r2, items, p.log)
    if skipped and not p.compare:
        unjudge(skipped)
    if not groups:
        p.log(f"{STAGE}: nothing to check")
        return {}
    sch = schema()
    first = p.call(STAGE, [(g.subject, g.item.as_of, user_prompt(g), sch) for g in groups])
    results: dict[int, dict] = {}
    recheck: list[tuple[Group, list[Cite]]] = []
    for g in groups:
        out, aid = first.get(g.subject, (None, None))
        verdicts = parse(out, len(g.cites))
        bad = []
        for n, c in enumerate(g.cites, 1):
            if n not in verdicts or aid is None:
                continue
            v, why = verdicts[n]
            results[c.citation_id] = {"analysis_id": aid, "first": v, "reason": why, "quote_found": c.quote_found}
            if v == "improper":
                bad.append(c)
        if bad:
            recheck.append((g, bad))
    if recheck:
        cfg = (p.rubric.get(STAGE) or {})
        saved = p.stages.get(STAGE)
        p.stages[STAGE] = cfg.get("confirm_tier") or saved
        try:
            second = p.call(STAGE, [(f"{g.subject}#2", g.item.as_of, user_prompt(g, bad, second=True), sch)
                                    for g, bad in recheck])
        finally:
            p.stages[STAGE] = saved
        for g, bad in recheck:
            out, aid = second.get(f"{g.subject}#2", (None, None))
            verdicts = parse(out, len(bad))
            for n, c in enumerate(bad, 1):
                if n in verdicts and aid is not None:
                    v, why = verdicts[n]
                    results[c.citation_id].update(second=v, second_analysis=aid,
                                                  reason=why if v != "improper" else results[c.citation_id]["reason"])
    if not p.compare:
        store(results)
    counts = {v: 0 for v in VERDICTS}
    for r in results.values():
        counts[settle(r["first"], r.get("second"))] += 1
    p.log(f"{STAGE}: {len(results)} citations judged: " + ", ".join(f"{v} {n}" for v, n in counts.items())
          + f"; {len(recheck)} groups re-checked")
    return results


def unjudge(citation_ids: list[int]) -> None:
    """Drop verdicts on citations that are no longer checked (their cited page holds another case)."""
    with db.connect() as c:
        c.execute("DELETE FROM citation_use WHERE citation_id = ANY(%s)", (citation_ids,))
        c.commit()


def store(results: dict[int, dict]) -> None:
    rows = [(cid, r["analysis_id"], r["first"], r.get("second"), r.get("second_analysis"),
             settle(r["first"], r.get("second")), r["reason"], r["quote_found"]) for cid, r in results.items()]
    for k in range(0, len(rows), 1000):
        with db.connect() as c:
            with c.cursor() as cur:
                cur.executemany(
                    "INSERT INTO citation_use (citation_id, analysis_id, first_verdict, second_verdict,"
                    " second_analysis, verdict, reason, quote_found) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (citation_id) DO UPDATE SET analysis_id = EXCLUDED.analysis_id,"
                    " first_verdict = EXCLUDED.first_verdict, second_verdict = EXCLUDED.second_verdict,"
                    " second_analysis = EXCLUDED.second_analysis, verdict = EXCLUDED.verdict,"
                    " reason = EXCLUDED.reason, quote_found = EXCLUDED.quote_found, checked_at = now()"
                    " WHERE EXISTS (SELECT 1 FROM citation WHERE id = EXCLUDED.citation_id)",
                    rows[k:k + 1000])
            c.commit()


# ---- scope and the free estimate ---------------------------------------------------

def scope_ids(conn, ids=None, types=None, years=None, unchecked=False, sample=0, seed=7) -> list[str]:
    q = "SELECT a.id FROM artifact a WHERE a.in_scope AND a.type = ANY(%s)"
    params: list = [list(types or TYPES)]
    if ids:
        q += " AND a.id = ANY(%s)"; params.append(ids)
    if years:
        lo, _, hi = years.partition("-")
        q += " AND extract(year FROM a.effective_date) BETWEEN %s AND %s"; params += [int(lo), int(hi or lo)]
    if unchecked:
        # Items with a checkable citation that has no verdict yet, or an unconfirmed "improper".
        q += (" AND EXISTS (SELECT 1 FROM citation c JOIN component f ON f.id = c.from_component"
              " JOIN artifact t ON t.id = c.to_artifact"
              " LEFT JOIN citation_use u ON u.citation_id = c.id"
              " WHERE f.artifact_id = a.id AND f.kind <> ALL(%s) AND c.to_artifact <> a.id"
              " AND (t.effective_date IS NULL OR t.effective_date < a.effective_date)"
              " AND (u.citation_id IS NULL OR (u.first_verdict = 'improper' AND u.second_verdict IS NULL)))")
        params.append(list(SKIP_KINDS))
    out = [r[0] for r in conn.execute(q + " ORDER BY a.effective_date, a.id", params).fetchall()]
    conn.commit()
    if sample and len(out) > sample:
        out = sorted(random.Random(seed).sample(out, sample))
    return out


# Token allowances per citation and per request (about 4 characters a token).
CITE_IN = (BEFORE_CHARS + AFTER_CHARS + EXCERPT_CHARS + 120) // 4
AUTH_IN = (SUMMARY_CHARS + 100) // 4
REQ_IN, SYSTEM_IN = 150, 900
CITE_OUT = 60
IMPROPER_SHARE = 0.06            # assumed share sent to the second check


def counts(conn, ids: list[str]) -> list[tuple]:
    """Per opinion: item id, item type, checkable citations, distinct authorities, and
    citations that cannot be checked (cited item not loaded)."""
    rows = conn.execute(
        "SELECT f.artifact_id, a.type, f.id,"
        " count(*) FILTER (WHERE t.id IS NOT NULL AND (t.effective_date IS NULL OR t.effective_date < a.effective_date)),"
        " count(DISTINCT c.to_artifact) FILTER (WHERE t.id IS NOT NULL),"
        " count(*) FILTER (WHERE t.id IS NULL)"
        " FROM citation c JOIN component f ON f.id = c.from_component JOIN artifact a ON a.id = f.artifact_id"
        " LEFT JOIN artifact t ON t.id = c.to_artifact"
        " WHERE f.artifact_id = ANY(%s) AND f.kind <> ALL(%s) AND c.to_artifact <> f.artifact_id"
        " GROUP BY 1, 2, 3", (ids, list(SKIP_KINDS))).fetchall()
    conn.commit()
    return rows


def estimate(rows: list[tuple], tier_first: str = "haiku", tier_second: str = "haiku",
             batch: bool = True, caching: bool = True, effort: str = "low") -> dict:
    out = {"items": set(), "opinions": 0, "citations": 0, "unloaded": 0, "requests": 0, "usd": 0.0,
           "by_type": {}}
    for aid, typ, _, n, auth, unloaded in rows:
        out["items"].add(aid)
        out["opinions"] += 1
        out["citations"] += n
        out["unloaded"] += unloaded
        if not n:
            continue
        reqs = math.ceil(n / CHUNK)
        tokens_in = reqs * (REQ_IN + SYSTEM_IN) + n * CITE_IN + min(auth, n) * AUTH_IN
        first = cost(tier_first, tokens_in, n * CITE_OUT, reqs * SYSTEM_IN if caching else 0, batch, effort)
        bad = n * IMPROPER_SHARE
        second = cost(tier_second, int(bad * (CITE_IN + AUTH_IN) + reqs * IMPROPER_SHARE * 2 * (REQ_IN + SYSTEM_IN)),
                      int(bad * CITE_OUT), 0, batch, effort)
        out["requests"] += reqs
        out["usd"] += first + second
        t = out["by_type"].setdefault(typ, {"items": set(), "citations": 0, "usd": 0.0})
        t["items"].add(aid)
        t["citations"] += n
        t["usd"] += first + second
    out["items"] = len(out["items"])
    for t in out["by_type"].values():
        t["items"] = len(t["items"])
    return out


def report(title: str, e: dict, scale: float = 1.0) -> list[str]:
    lo, hi = RANGE
    usd = e["usd"] * scale
    lines = [f"== {title}",
             f"items: {e['items'] * scale:,.0f}  opinions: {e['opinions'] * scale:,.0f}"
             f"  citations checked: {e['citations'] * scale:,.0f}  requests: {e['requests'] * scale:,.0f}"
             f"  cited but not loaded (not checked): {e['unloaded'] * scale:,.0f}"]
    for typ, t in sorted(e["by_type"].items()):
        lines.append(f"  {typ:20} items {t['items'] * scale:>8,.0f}  citations {t['citations'] * scale:>9,.0f}"
                     f"   ${t['usd'] * scale:>8,.2f}")
    lines.append(f"  total ${usd:,.2f}  (range ${usd * lo:,.2f} to ${usd * hi:,.2f})")
    return lines

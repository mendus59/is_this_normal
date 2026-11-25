"""Paid: a short plain-language summary of each item, shown to readers at the top of its
page. One Haiku call per item reads the item's own free material: its title, its official
summary (the Reporter's syllabus, the CRS summary or the Federal Register abstract) and the
start of its text. Nothing else is sent, so nothing dated after the item reaches the model.

Results are cached in `analysis` (kind `overview`) by a hash of their input like every
other stage; the newest non-trial row per item is the published summary.
"""

import math
import re
from concurrent.futures import ThreadPoolExecutor

from itn import db
from itn.analyze import data
from itn.analyze.estimate import CHARS_PER_TOKEN, cost

STAGE = "overview"
TYPES = ("scotus_case", "public_law", "executive_document", "action_event")
OFFICIAL_CHARS = 6_000
ITEM_CHARS = 14_000          # official summary plus the start of the text
MAX_WORDS = 70
READERS = 16

SYSTEM = f"""You write short plain-language summaries of U.S. Supreme Court rulings,
federal laws and presidential documents for IsThisNormal, a nonpartisan project. Readers
are members of the public with no legal training.

Write two or three sentences, at most {MAX_WORDS} words in all, that say:
- what the item does or decides, in concrete terms (for a ruling: who won and the rule
  the Court adopted; for a law or presidential document: what it requires, allows,
  creates, funds or changes);
- who or what it mainly affects.

Rules:
- Use only the material given. Do not add what you know about later events, later cases,
  or how the item was received, and do not say whether it was later changed or overturned.
- Plain words. Explain or avoid legal terms ("certiorari", "remand", "appropriations").
  No citations, section numbers, docket numbers or case reporter references.
- Neutral and factual: describe, do not judge. Do not say whether it is normal, unusual,
  good or bad, and do not use loaded words for either side.
- Do not start with the item's title or "This ruling", "This order" and the like: start
  with the substance (for example "The Court held that ...", "Creates ...", "Directs ...").
- If the material is too thin to tell what the item does, say so in one sentence.

Answer only with the requested JSON."""


def schema() -> dict:
    return {"type": "object", "properties": {"summary": {"type": "string"}},
            "required": ["summary"], "additionalProperties": False}


KIND = {"scotus_case": "Supreme Court ruling", "public_law": "federal law",
        "executive_document": "presidential document", "action_event": "presidential action"}


def material(item, text: str) -> str:
    """The item's official summary, then the start of its text. The syllabus is often also
    the first part of a ruling's text: it is sent once."""
    official = (item.summary or "").strip()[:OFFICIAL_CHARS]
    text = (text or "").strip()
    if official and official[:200] in text[: OFFICIAL_CHARS + 2_000]:
        official = ""
    room = max(0, ITEM_CHARS - len(official))
    parts = []
    if official:
        parts.append(f"Official summary:\n{official}")
    if text:
        parts.append(f"Text (start):\n{text[:room]}")
    return "\n\n".join(parts) or "(no summary or text available)"


HELD_CHARS = 7_000
OPENING_CHARS = 2_500
ENDING_CHARS = 3_000
_HELD = re.compile(r"\bHeld\s*:")


def ruling_material(item, parts: list[str]) -> str:
    """A ruling's facts, outcome and conclusion: the start of the syllabus (the facts and
    question), its "Held:" part (the holding), and the end of the last controlling opinion
    (the disposition). The start of a long ruling alone is mostly background."""
    official = (item.summary or "").strip()
    parts = [t.strip() for t in parts if t and t.strip()]
    joined = "\n\n".join(parts)
    source = official if _HELD.search(official) else joined
    m = _HELD.search(source)
    if not m and not official:
        return material(item, joined)
    opening = (official or joined)[:OPENING_CHARS]
    out = [f"Syllabus or summary (start):\n{opening}"]
    if m and m.start() >= OPENING_CHARS - 200:
        out.append(f"Holding (from the syllabus):\n{source[m.start():m.start() + HELD_CHARS]}")
    elif m:
        out[0] = f"Syllabus or summary:\n{source[:m.start() + HELD_CHARS]}"
    if parts and len(parts[-1]) > ENDING_CHARS:
        out.append(f"End of the Court's opinion:\n{parts[-1][-ENDING_CHARS:]}")
    out.append("Say what the Court decided (the holding and who won), not only the background.")
    return "\n\n".join(out)


def user_prompt(item, text) -> str:
    """`text` is the item's text, or for a ruling the list of its controlling opinions' texts."""
    sub = f", {item.subtype.replace('_', ' ')}" if item.subtype else ""
    return (f"Item {item.id}: {item.title}\n"
            f"Kind: {KIND.get(item.type, item.type.replace('_', ' '))}{sub}; effective {item.effective_date}.\n\n"
            f"{ruling_material(item, text) if isinstance(text, list) else material(item, text)}\n\n"
            f"Write the plain-language summary (two or three sentences, at most {MAX_WORDS} words).")


def clean(output: dict | None) -> str | None:
    s = " ".join(((output or {}).get("summary") or "").split())
    return s[:800] or None


# ---- scope -------------------------------------------------------------------------

def scope_ids(conn, ids=None, types=None, years=None, missing=False) -> list[str]:
    q = "SELECT a.id FROM artifact a WHERE a.in_scope AND a.type = ANY(%s)"
    params: list = [list(types or TYPES)]
    if ids:
        q += " AND a.id = ANY(%s)"; params.append(ids)
    if years:
        lo, _, hi = years.partition("-")
        q += " AND extract(year FROM a.effective_date) BETWEEN %s AND %s"; params += [int(lo), int(hi or lo)]
    if missing:
        q += (" AND NOT EXISTS (SELECT 1 FROM analysis an WHERE an.kind = %s AND an.subject_id = a.id"
              " AND an.rubric_version NOT LIKE '%%-compare')")
        params.append(STAGE)
    out = [r[0] for r in conn.execute(q + " ORDER BY a.effective_date, a.id", params).fetchall()]
    conn.commit()
    return out


def current(conn, ids: list[str] | None = None) -> dict[str, str]:
    """Published summary per item: the newest non-trial `overview` analysis."""
    q = ("SELECT DISTINCT ON (subject_id) subject_id, output FROM analysis"
         " WHERE kind = %s AND rubric_version NOT LIKE '%%-compare'")
    params: list = [STAGE]
    if ids is not None:
        q += " AND subject_id = ANY(%s)"; params.append(ids)
    rows = conn.execute(q + " ORDER BY subject_id, id DESC", params).fetchall()
    conn.commit()
    return {aid: s for aid, out in rows if (s := clean(out))}


def read(r2, item):
    """What the prompt needs: the text start, or for a ruling each controlling opinion whole."""
    if item.type == "scotus_case":
        return [r2.get_text(k) or "" for k in item.text_keys]
    return data.text_of(r2, item.text_keys, ITEM_CHARS)


# ---- the run -----------------------------------------------------------------------

def run(p, ids: list[str], show: int = 50) -> dict[str, str]:
    """Summarize `ids` with the pipeline `p` (cache, batches, budget). Prints every summary
    when there are at most `show` items, so a pilot can be read from the run log."""
    items = []
    for k in range(0, len(ids), 500):
        with db.connect() as c:
            items += data.load_items(c, ids[k:k + 500])
    with ThreadPoolExecutor(READERS) as pool:
        texts = dict(zip([i.id for i in items], pool.map(lambda i: read(p.r2, i), items)))
    sch = schema()
    got = p.call(STAGE, [(i.id, i.as_of, user_prompt(i, texts[i.id]), sch) for i in items])
    out = {i.id: s for i in items if (s := clean(got.get(i.id, (None, None))[0]))}
    p.log(f"{STAGE}: {len(out)} of {len(items)} items summarized")
    if len(items) <= show:
        for i in items:
            p.log(f"\n[{i.id}] {i.title} ({i.type}, {i.effective_date})\n  {out.get(i.id, '(no summary)')}")
    return out


# ---- the free estimate -------------------------------------------------------------

SYSTEM_IN = len(SYSTEM) // CHARS_PER_TOKEN
REQ_IN = 80
OUT = 110                     # about 70 words of JSON


def sizes(conn, ids: list[str]) -> list[tuple[str, int]]:
    """Item type and an upper bound on the characters its prompt carries (no text reads:
    every item is assumed to fill ITEM_CHARS, so short documents make the real cost lower)."""
    rows = conn.execute("SELECT type FROM artifact WHERE id = ANY(%s)", (ids,)).fetchall()
    conn.commit()
    return [(t, ITEM_CHARS) for (t,) in rows]


def estimate(rows: list[tuple[str, int]], tier: str = "haiku", batch: bool = True, caching: bool = True) -> dict:
    out = {"items": 0, "usd": 0.0, "by_type": {}}
    for typ, chars in rows:
        tokens_in = SYSTEM_IN + REQ_IN + math.ceil(chars / CHARS_PER_TOKEN)
        usd = cost(tier, tokens_in, OUT, SYSTEM_IN if caching else 0, batch, "low")
        out["items"] += 1
        out["usd"] += usd
        t = out["by_type"].setdefault(typ, {"items": 0, "usd": 0.0})
        t["items"] += 1
        t["usd"] += usd
    return out

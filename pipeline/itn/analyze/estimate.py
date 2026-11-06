"""Free cost estimate for a scoring run. Calls no model and needs no API key.

Token counts are approximated from stored text sizes (about 4 characters per token)
and fixed allowances for prompts and outputs, so treat the result as a range. Prices
are the published per-million-token rates as of 2026-10-06; check them before a large
run."""

import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from itn.analyze.presort import routine_reason
from itn.extract.citations import extract

CHARS_PER_TOKEN = 4
# $ per million tokens (input, output). Haiku's rate rises above 100K input tokens.
PRICES = {"haiku": (0.10, 0.50), "sonnet": (2.00, 10.00), "opus": (4.00, 20.00)}
HAIKU_LONG = (0.50, 2.50)
HAIKU_LONG_FROM = 100_000
# Thinking is billed as output; allowance on top of the visible output.
THINKING = {"haiku": 1.5, "sonnet": 2.0, "opus": 2.0}
TIERS = ["haiku", "sonnet", "opus"]

PROMPT = 2_000              # rubric and instructions per call
SUMMARY_MAX_IN = 150_000
SUMMARY_OUT = 800
CHECK_IN, CHECK_OUT = 3_000, 300        # citing context + digest of the cited item
MAX_CHECKS_PER_ITEM = 60
DIGEST_MAX_IN, DIGEST_OUT = 40_000, 1_500
ANALOG_IN, ANALOG_OUT = 9_000, 1_500    # item summary + about 12 candidate summaries
DIGESTS_IN_ASSESSMENT = 15
ASSESS_OUT = 3_000
VERIFY_SHARE = 0.4                      # assumed share rated novel or unprecedented
VERIFY_OUT = 2_000
RANGE = (0.7, 1.6)

CONTROLLING = {"majority", "plurality", "unanimous", "opinion", "per_curiam", "lead", "syllabus"}


def cost(tier: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = PRICES[tier]
    if tier == "haiku" and tokens_in > HAIKU_LONG_FROM:
        p_in, p_out = HAIKU_LONG
    return (tokens_in * p_in + tokens_out * THINKING[tier] * p_out) / 1e6


def cap_tier(tier: str, max_tier: str) -> str:
    return TIERS[min(TIERS.index(tier), TIERS.index(max_tier))]


@dataclass
class Item:
    id: str
    type: str
    title: str
    text_tokens: int = 0
    cited: list[str] = field(default_factory=list)   # distinct cited artifact ids
    presort: str | None = None


@dataclass
class Estimate:
    calls: dict = field(default_factory=dict)        # stage -> count
    usd: dict = field(default_factory=dict)          # stage -> dollars
    items: list = field(default_factory=list)
    digests: int = 0
    unloaded_cited: int = 0

    def add(self, stage: str, n: int, usd: float) -> None:
        self.calls[stage] = self.calls.get(stage, 0) + n
        self.usd[stage] = self.usd.get(stage, 0.0) + usd

    @property
    def total(self) -> float:
        return sum(self.usd.values())


def plan(items: list[Item], digest_sizes: dict[str, int], stages: dict, max_tier: str) -> Estimate:
    """Cost of scoring `items`. digest_sizes maps each loaded cited artifact to its text
    size in tokens; cited ids missing from it are not loaded and get no digest."""
    t = {s: cap_tier(tier, max_tier) for s, tier in stages.items()}
    est = Estimate(items=items)
    need_digest = set()
    for it in items:
        if it.presort:
            continue
        s_in = min(it.text_tokens, SUMMARY_MAX_IN) + 1_000
        est.add("summary", 1, cost(t["summary"], s_in, SUMMARY_OUT))
        checks = min(len(it.cited), MAX_CHECKS_PER_ITEM)
        est.add("citation_check", checks, checks * cost(t["citation_check"], CHECK_IN, CHECK_OUT))
        loaded = [c for c in it.cited if c in digest_sizes]
        need_digest.update(loaded)
        est.unloaded_cited += len(it.cited) - len(loaded)
        est.add("analog_assessment", 1, cost(t["analog_assessment"], ANALOG_IN, ANALOG_OUT))
        a_in = PROMPT + 1_000 + min(len(loaded), DIGESTS_IN_ASSESSMENT) * DIGEST_OUT + checks * 80 + ANALOG_OUT
        est.add("type_assessment", 1, cost(t["type_assessment"], a_in, ASSESS_OUT))
        est.add("verification", VERIFY_SHARE, VERIFY_SHARE * cost(t["verification"], a_in + ASSESS_OUT, VERIFY_OUT))
    for c in need_digest:
        d_in = min(digest_sizes[c], DIGEST_MAX_IN) + 1_500
        est.add("authority_digest", 1, cost(t["authority_digest"], d_in, DIGEST_OUT))
    est.digests = len(need_digest)
    return est


# ---- gathering inputs from the database and R2 -------------------------------------

def _keys(conn, ids: list[str]) -> dict[str, list[str]]:
    """Text keys to read for each artifact: its controlling opinions, else its own text."""
    out: dict[str, list[str]] = {i: [] for i in ids}
    rows = conn.execute("SELECT artifact_id, kind, text_key FROM component"
                        " WHERE artifact_id = ANY(%s) AND text_key IS NOT NULL", (ids,)).fetchall()
    by: dict[str, list[tuple[str, str]]] = {}
    for aid, kind, key in rows:
        by.setdefault(aid, []).append((kind, key))
    for aid, comps in by.items():
        lead = [k for kind, k in comps if kind in CONTROLLING]
        out[aid] = lead or [k for kind, k in comps if kind != "dissent"]
    for aid, key in conn.execute("SELECT id, text_key FROM artifact WHERE id = ANY(%s)"
                                 " AND text_key IS NOT NULL", (ids,)).fetchall():
        if not out.get(aid):
            out[aid] = [key]
    return out


def _sizes(r2, keys: dict[str, list[str]]) -> dict[str, int]:
    flat = sorted({k for ks in keys.values() for k in ks})
    with ThreadPoolExecutor(24) as pool:
        size = dict(zip(flat, pool.map(r2.size, flat)))
    return {aid: sum(size[k] for k in ks) // CHARS_PER_TOKEN for aid, ks in keys.items()}


def gather(conn, r2, ids: list[str]) -> tuple[list[Item], dict[str, int]]:
    rows = conn.execute("SELECT id, type, subtype, title, text_key FROM artifact WHERE id = ANY(%s)",
                        (ids,)).fetchall()
    items = {r[0]: Item(id=r[0], type=r[1], title=r[3],
                        presort=routine_reason(r[2], r[3]) if r[1] == "executive_document" else None)
             for r in rows}
    keys = _keys(conn, list(items))
    for aid, n in _sizes(r2, keys).items():
        items[aid].text_tokens = n
    cites = conn.execute(
        "SELECT DISTINCT f.artifact_id, c.to_artifact FROM citation c"
        " JOIN component f ON f.id = c.from_component"
        " WHERE f.artifact_id = ANY(%s) AND (f.kind = ANY(%s) OR NOT EXISTS ("
        "   SELECT 1 FROM component g WHERE g.artifact_id = f.artifact_id AND g.kind = ANY(%s)))",
        (list(items), list(CONTROLLING), list(CONTROLLING))).fetchall()
    for aid, to in cites:
        items[aid].cited.append(to)
    # Executive documents and laws have no extracted citations yet: read them from the text.
    for it in items.values():
        if it.cited or it.presort or not keys.get(it.id):
            continue
        text = "\n".join(r2.get_text(k) or "" for k in keys[it.id])
        it.cited = sorted({row["to_artifact"] for row in extract(text)} - {it.id})
    cited = sorted({c for it in items.values() for c in it.cited})
    loaded = [r[0] for r in conn.execute("SELECT id FROM artifact WHERE id = ANY(%s)", (cited,)).fetchall()]
    conn.commit()
    digest_sizes = _sizes(r2, _keys(conn, loaded)) if loaded else {}
    return [items[i] for i in ids if i in items], digest_sizes


def resolve_anchor(conn, typ: str, a: dict) -> str | None:
    if a.get("id"):
        row = conn.execute("SELECT id FROM artifact WHERE id = %s", (a["id"],)).fetchone()
        if row:
            return row[0]
    if a.get("title"):
        q = ("SELECT id FROM artifact WHERE type = %s AND in_scope AND title ILIKE %s"
             " AND (%s::int IS NULL OR extract(year FROM effective_date) = %s::int)"
             " AND (%s::text IS NULL OR subtype = %s::text)"
             " ORDER BY effective_date, id LIMIT 1")
        row = conn.execute(q, (typ, f"%{a['title']}%", a.get("year"), a.get("year"),
                               a.get("subtype"), a.get("subtype"))).fetchone()
        if row:
            return row[0]
    return None


def sample_ids(conn, typ: str, n: int, seed: int = 7) -> tuple[list[str], int]:
    """A reproducible random sample of in-scope ids of one type, and the type's total."""
    all_ids = [r[0] for r in conn.execute(
        "SELECT id FROM artifact WHERE type = %s AND in_scope ORDER BY id", (typ,)).fetchall()]
    conn.commit()
    rng = random.Random(seed)
    return (rng.sample(all_ids, n) if len(all_ids) > n else all_ids), len(all_ids)


def report(title: str, est: Estimate) -> list[str]:
    lo, hi = RANGE
    lines = [f"== {title}",
             f"items: {len(est.items)}  pre-sorted routine: {sum(1 for i in est.items if i.presort)}"
             f"  authority digests: {est.digests}  cited but not loaded: {est.unloaded_cited}"]
    for stage in ["summary", "citation_check", "authority_digest", "analog_assessment",
                  "type_assessment", "verification"]:
        if stage in est.calls:
            lines.append(f"  {stage:18} calls {est.calls[stage]:>9,.0f}   ${est.usd[stage]:>9,.2f}")
    lines.append(f"  total ${est.total:,.2f}  (range ${est.total * lo:,.2f} to ${est.total * hi:,.2f};"
                 f" Batch API about half: ${est.total / 2:,.2f})")
    return lines

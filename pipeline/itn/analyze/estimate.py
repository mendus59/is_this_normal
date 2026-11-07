"""Free cost estimate for a scoring run. Calls no model and needs no API key.

Token counts are approximated from stored text sizes (about 4 characters per token)
and fixed allowances for prompts and outputs, so treat the result as a range. Prices
are the published per-million-token rates as of 2026-10-06; check them before a large
run."""

import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from itn import db
from itn.analyze.presort import routine_reason
from itn.extract.citations import extract, extract_statutes

CHARS_PER_TOKEN = 4
# $ per million tokens (input, output). Haiku's rate rises above 100K input tokens.
PRICES = {"haiku": (0.10, 0.50), "sonnet": (2.00, 10.00), "opus": (4.00, 20.00)}
HAIKU_LONG = (0.50, 2.50)
HAIKU_LONG_FROM = 100_000
# Thinking is billed as output: allowance on top of the visible output, by effort level.
THINKING = {"low": 1.2, "medium": 1.5, "high": 2.0}
EFFORT = {"summary": "low", "authority_digest": "medium", "citation_check": "low",
          "type_assessment": "medium", "verification": "high"}
ESCALATE_SHARE = 0.4    # comparison: share of Haiku assessments re-run on Sonnet
TIERS = ["haiku", "sonnet", "opus"]

PROMPT = 2_000              # rubric and instructions per call
SUMMARY_MAX_IN = 150_000
SUMMARY_OUT = 800
CHECK_IN, CHECK_OUT = 2_000, 300        # citing context + the cited item's free syllabus or summary
MAX_CHECKS_PER_ITEM = 60
DIGEST_MAX_IN, DIGEST_OUT = 40_000, 1_500
ANALOG_IN = 7_000                       # about 12 comparable earlier items, read by the assessment
DIGESTS_IN_ASSESSMENT = 15             # digests are written only for each item's most-cited authorities
ASSESS_OUT = 3_000
VERIFY_SHARE = 0.4                      # assumed share rated novel or unprecedented
VERIFY_OUT = 2_000
RANGE = (0.7, 1.6)
EYECITE_MAX_CHARS = 300_000

CONTROLLING = {"majority", "plurality", "unanimous", "opinion", "per_curiam", "lead", "syllabus"}


CACHE_READ = 0.1      # cached input costs a tenth of the input price
BATCH = 0.5           # Batch API halves both input and output


def cost(tier: str, tokens_in: int, tokens_out: int, cached: int = 0, batch: bool = False,
         effort: str = "high") -> float:
    """cached: how many of tokens_in are a cache hit (the shared rubric prefix)."""
    p_in, p_out = PRICES[tier]
    if tier == "haiku" and tokens_in > HAIKU_LONG_FROM:
        p_in, p_out = HAIKU_LONG
    usd = ((tokens_in - cached) * p_in + cached * p_in * CACHE_READ
           + tokens_out * THINKING[effort] * p_out) / 1e6
    return usd * (BATCH if batch else 1.0)


_cost = cost


def cap_tier(tier: str, max_tier: str) -> str:
    return TIERS[min(TIERS.index(tier), TIERS.index(max_tier))]


@dataclass
class Item:
    id: str
    type: str
    title: str
    text_tokens: int = 0
    cited: list[str] = field(default_factory=list)   # distinct cited artifact ids, most cited first
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


def plan(items: list[Item], digest_sizes: dict[str, int], stages: dict, max_tier: str,
         batch: bool = False, caching: bool = False, effort: dict | None = None,
         escalate_to: str | None = None) -> Estimate:
    """Cost of scoring `items`. digest_sizes maps each loaded cited artifact to its text
    size in tokens; cited ids missing from it are not loaded and get no digest."""
    t = {s: cap_tier(tier, max_tier) for s, tier in stages.items()}
    est = Estimate(items=items)
    hit = PROMPT if caching else 0

    eff = {**EFFORT, **(effort or {})}

    def cost(tier, tokens_in, tokens_out, cached=0, stage=None):
        return _cost(tier, tokens_in, tokens_out, cached, batch, eff.get(stage, "high"))
    need_digest = set()
    for it in items:
        if it.presort:
            continue
        s_in = min(it.text_tokens, SUMMARY_MAX_IN) + 1_000
        est.add("summary", 1, cost(t["summary"], s_in, SUMMARY_OUT, stage="summary"))  # no shared prefix worth caching
        checks = min(len(it.cited), MAX_CHECKS_PER_ITEM)
        est.add("citation_check", checks, checks * cost(t["citation_check"], CHECK_IN, CHECK_OUT, min(hit, CHECK_IN // 2), "citation_check"))
        loaded = [c for c in it.cited if c in digest_sizes]
        need_digest.update(loaded[:DIGESTS_IN_ASSESSMENT])
        est.unloaded_cited += len(it.cited) - len(loaded)
        # The assessment reads the comparable earlier items itself (no separate analog call).
        a_in = PROMPT + 1_000 + min(len(loaded), DIGESTS_IN_ASSESSMENT) * DIGEST_OUT + checks * 80 + ANALOG_IN
        est.add("type_assessment", 1, cost(t["type_assessment"], a_in, ASSESS_OUT, hit, "type_assessment"))
        if escalate_to:
            esc = cap_tier(escalate_to, max_tier)
            est.add("type_assessment", ESCALATE_SHARE,
                    ESCALATE_SHARE * cost(esc, a_in, ASSESS_OUT, hit, "type_assessment"))
        est.add("verification", VERIFY_SHARE,
                VERIFY_SHARE * cost(t["verification"], a_in + ASSESS_OUT, VERIFY_OUT, hit, "verification"))
    for c in need_digest:
        d_in = min(digest_sizes[c], DIGEST_MAX_IN) + 1_500
        est.add("authority_digest", 1, cost(t["authority_digest"], d_in, DIGEST_OUT, stage="authority_digest"))
    est.digests = len(need_digest)
    return est


# Economy profile (rubric `economy:`): one Haiku assessment per item from free material.
ECON_ASSESS_OUT = 1_200          # one short reason per dimension
ECON_CHECK_IN = 1_000
ECON_VERIFY_SHARE = 0.4          # items rated unusual: checked and given a second pass


def plan_economy(items: list[Item], cfg: dict, batch: bool = True, caching: bool = True) -> Estimate:
    est = Estimate(items=items)
    hit = PROMPT if caching else 0
    eff = cfg.get("effort") or {}
    c = lambda s_in, s_out, cached, stage: _cost("haiku", s_in, s_out, cached, batch, eff.get(stage, "low"))
    for it in items:
        if it.presort:
            continue
        item_in = min(it.text_tokens, cfg["item_chars"] // CHARS_PER_TOKEN)
        cited = min(len(it.cited), cfg["max_cited"]) * (cfg["cited_chars"] // CHARS_PER_TOKEN + 30)
        analogs = cfg["analogs"] * (cfg["analog_chars"] // CHARS_PER_TOKEN + 40)
        a_in = PROMPT + 300 + item_in + cited + analogs
        est.add("type_assessment", 1, c(a_in, ECON_ASSESS_OUT, hit, "type_assessment"))
        checks = min(len(it.cited), cfg["max_checks"])
        est.add("citation_check", ECON_VERIFY_SHARE * checks,
                ECON_VERIFY_SHARE * checks * c(ECON_CHECK_IN, CHECK_OUT, min(hit, ECON_CHECK_IN // 2), "citation_check"))
        est.add("verification", ECON_VERIFY_SHARE,
                ECON_VERIFY_SHARE * c(a_in + ECON_ASSESS_OUT + checks * 60, ECON_ASSESS_OUT, hit, "verification"))
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
    conn.commit()  # R2 reads follow; don't hold a transaction open while they run
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
    conn.commit()
    keys = _keys(conn, list(items))
    for aid, n in _sizes(r2, keys).items():
        items[aid].text_tokens = n
    cites = conn.execute(
        "SELECT f.artifact_id, c.to_artifact FROM citation c"
        " JOIN component f ON f.id = c.from_component"
        " WHERE f.artifact_id = ANY(%s) AND (f.kind = ANY(%s) OR NOT EXISTS ("
        "   SELECT 1 FROM component g WHERE g.artifact_id = f.artifact_id AND g.kind = ANY(%s)))"
        " GROUP BY 1, 2 ORDER BY 1, count(*) DESC, 2",
        (list(items), list(CONTROLLING), list(CONTROLLING))).fetchall()
    conn.commit()
    for aid, to in cites:
        items[aid].cited.append(to)
    # Executive documents and laws have no extracted citations yet: read them from the text.
    todo = [it for it in items.values() if not (it.cited or it.presort or not keys.get(it.id))]

    def cites_in(it: Item) -> list[str]:
        text = "\n".join(r2.get_text(k) or "" for k in keys[it.id])
        order = extract_statutes(text)
        if len(text) <= EYECITE_MAX_CHARS:  # eyecite takes minutes on omnibus laws
            order += [r["to_artifact"] for r in extract(text)]
        return [c for c in dict.fromkeys(order) if c != it.id]

    with ThreadPoolExecutor(8) as pool:
        for it, found in zip(todo, pool.map(cites_in, todo)):
            it.cited = found
    cited = sorted({c for it in items.values() for c in it.cited})
    # The reads above can outlast the database's idle timeout, so use a fresh connection.
    with db.connect() as fresh:
        loaded = [r[0] for r in fresh.execute("SELECT id FROM artifact WHERE id = ANY(%s)", (cited,)).fetchall()]
        loaded_keys = _keys(fresh, loaded) if loaded else {}
    digest_sizes = _sizes(r2, loaded_keys) if loaded else {}
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
    for stage in ["summary", "citation_check", "authority_digest", "type_assessment", "verification"]:
        if stage in est.calls:
            lines.append(f"  {stage:18} calls {est.calls[stage]:>9,.0f}   ${est.usd[stage]:>9,.2f}")
    lines.append(f"  total ${est.total:,.2f}  (range ${est.total * lo:,.2f} to ${est.total * hi:,.2f})")
    return lines

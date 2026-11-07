"""The paid scoring pipeline. Runs only under `itn analyze` without --dry-run, in the
`paid` GitHub environment. Every model output is cached in `analysis` by a hash of its
inputs, so a re-run (or a run stopped by its budget) never pays twice for the same call.

Stages, each sent as one batch across all items:
  summary -> authority_digest -> citation_check -> analog_assessment -> type_assessment
  -> combine (same-actor rule, free) -> verification for low scores -> final score.
"""

import hashlib
import json
from math import floor

from psycopg.types.json import Jsonb

from itn import db
from itn.analyze import data, prompts
from itn.analyze.actors import final_rating
from itn.analyze.llm import BudgetExceeded, Req, Spend, run as run_requests, tier_of

MAX_DIGESTS_PER_ITEM = 15
MAX_CHECKS_PER_ITEM = 60
BAD_VERDICTS = {"contradicts", "mischaracterized"}
VERIFY_IF_FINAL_AT_MOST = 1
VERIFY_IF_RAW_BELOW = 0.5
DISAGREEMENT = 2
DISAGREEMENT_CONFIDENCE = 0.7


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Pipeline:
    def __init__(self, r2, client, rubric: dict, budget: float, max_tier: str, compare: bool = False,
                 dispatched_by: str | None = None, scope: dict | None = None, log=print):
        self.r2, self.client, self.rubric, self.log = r2, client, rubric, log
        self.compare = compare
        self.stages = {**rubric["stages"], **(rubric.get("compare_stages", {}) if compare else {})}
        self.max_tier = max_tier
        self.models = rubric["models"]
        self.rv = f"{rubric['version']}{'-compare' if compare else ''}"
        run_cfg = rubric.get("run", {})
        self.batch, self.caching = bool(run_cfg.get("batch")), bool(run_cfg.get("prompt_caching"))
        self.spend = Spend(budget)
        with db.connect() as c:
            self.run_id = c.execute(
                "INSERT INTO run (dispatched_by, scope, budget_usd, dry_run) VALUES (%s, %s, %s, false)"
                " RETURNING id", (dispatched_by, Jsonb({**(scope or {}), "rubric": self.rv}), budget)).fetchone()[0]

    # ---- cache-aware calls -------------------------------------------------------

    def _tier(self, stage: str) -> str:
        return tier_of(self.stages[stage], self.max_tier)

    def call(self, stage: str, jobs: list[tuple[str, object, str, dict]]) -> dict[str, tuple[dict | None, int | None]]:
        """jobs: (subject_id, as_of, user prompt, schema). Returns subject -> (output, analysis id)."""
        tier = self._tier(stage)
        system = prompts.system_prompt(self.rubric, stage)
        keyed = {s: (a, u, sch, _hash(stage, self.rv, self.models[tier], system, u)) for s, a, u, sch in jobs}
        out: dict[str, tuple[dict | None, int | None]] = {}
        by_hash = {k[3]: s for s, k in keyed.items()}
        with db.connect() as c:
            for aid, h, output in c.execute(
                    "SELECT id, input_hash, output FROM analysis WHERE kind = %s AND input_hash = ANY(%s)",
                    (stage, list(by_hash))).fetchall():
                out[by_hash[h]] = (output, aid)
        todo = [s for s in keyed if s not in out]
        if todo:
            self.log(f"{stage}: {len(todo)} calls on {tier} ({len(out)} cached)")
            reqs = [Req(f"{stage[:8]}-{i}", stage, tier, system, keyed[s][1], keyed[s][2]) for i, s in enumerate(todo)]
            subject = {r.custom_id: s for r, s in zip(reqs, todo)}
            results = run_requests(self.client, reqs, self.models, self.spend, self.batch, self.caching)
            with db.connect() as c:
                for cid, res in results.items():
                    s = subject[cid]
                    if res.data is None:
                        self.log(f"  {stage} {s}: {res.error}")
                        out[s] = (None, None)
                        continue
                    row = c.execute(
                        "INSERT INTO analysis (kind, subject_id, as_of, input_hash, rubric_version, model, output,"
                        " cost_usd, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (kind, input_hash) DO UPDATE SET output = EXCLUDED.output RETURNING id",
                        (stage, s, keyed[s][0], keyed[s][3], self.rv, res.model, Jsonb(res.data),
                         round(res.cost_usd, 4), self.run_id)).fetchone()
                    out[s] = (res.data, row[0])
                self._save_run(c, "running")
        return out

    def _save_run(self, c, status: str) -> None:
        t = self.spend.tokens
        c.execute("UPDATE run SET input_tokens = %s, cached_tokens = %s, output_tokens = %s, cost_usd = %s,"
                  " status = %s, finished_at = CASE WHEN %s <> 'running' THEN now() END WHERE id = %s",
                  (t["input"], t["cached"], t["output"], round(self.spend.spent, 4), status, status, self.run_id))

    # ---- the pipeline -------------------------------------------------------------

    def score(self, ids: list[str]) -> None:
        status = "done"
        try:
            self._score(ids)
        except BudgetExceeded as e:
            self.log(str(e))
            status = "stopped_budget"
        with db.connect() as c:
            self._save_run(c, status)
        self.log(f"Run {self.run_id} {status}: ${self.spend.spent:.2f} of ${self.spend.budget:.2f}")

    def _score(self, ids: list[str]) -> None:
        with db.connect() as c:
            items = data.load_items(c, ids)
        if not self.compare:
            self._write_presorted([i for i in items if i.presort])
        items = [i for i in items if not i.presort]
        if not items:
            return
        texts = {i.id: data.text_of(self.r2, i.text_keys, data.TEXT_MAX_CHARS) for i in items}
        with db.connect() as c:
            cites = {i.id: data.citations(c, self.r2, i, texts[i.id]) for i in items}
            candidates = {i.id: data.analog_candidates(c, i) for i in items}

        summaries = self.call("summary", [(i.id, i.as_of, *prompts.summary(i, texts[i.id])) for i in items])
        summary = {i.id: (summaries[i.id][0] or {}).get("summary") or (i.summary or i.title) for i in items}

        # Digests of each item's most-cited authorities (shared across items).
        wanted = {}
        for i in items:
            for ct in cites[i.id][:MAX_DIGESTS_PER_ITEM]:
                wanted.setdefault(ct["to"], ct)
        with db.connect() as c:
            keys = {a: data._text_keys(c, a) for a in wanted}
        jobs = []
        for a, ct in wanted.items():
            text = data.text_of(self.r2, keys[a], data.AUTHORITY_TEXT_MAX_CHARS) if keys[a] else ""
            if text.strip():
                jobs.append((a, None, *prompts.authority_digest(a, ct["title"], ct["date"], text)))
        digests = self.call("authority_digest", jobs)
        digest = {a: (o or {}).get("digest") for a, (o, _) in digests.items()}

        def material(ct):
            return digest.get(ct["to"]) or ct["summary"] or ct["title"]

        check_jobs, check_meta = [], {}
        for i in items:
            for ct in cites[i.id][:MAX_CHECKS_PER_ITEM]:
                if not ct["context"]:
                    continue
                sid = f"{i.id}->{ct['to']}"
                check_meta[sid] = (i, ct)
                check_jobs.append((sid, i.as_of, *prompts.citation_check(i, ct, material(ct))))
        checks = self.call("citation_check", check_jobs)
        self._second_check(checks, check_meta, material)

        analogs = self.call("analog_assessment", [
            (i.id, i.as_of, *prompts.analog_assessment(i, summary[i.id], candidates[i.id])) for i in items])

        assess_users, assess_jobs, allowed = {}, [], {}
        for i in items:
            top = cites[i.id][:MAX_DIGESTS_PER_ITEM]
            dg = {ct["to"]: material(ct) for ct in top}
            ck = {s.split("->", 1)[1]: (checks.get(s, (None,))[0] or {}).get("verdict")
                  for s in check_meta if s.startswith(i.id + "->")}
            an = analogs.get(i.id, (None,))[0] or {}
            same = [cd["id"] for cd in candidates[i.id] if cd["same_actor"]]
            user, schema = prompts.type_assessment(self.rubric, i, summary[i.id], dg, ck, an, same)
            assess_users[i.id] = user
            allowed[i.id] = {i.id} | {ct["to"] for ct in cites[i.id]} | {cd["id"] for cd in candidates[i.id]}
            assess_jobs.append((i.id, i.as_of, user, schema))
        assessed = self.call("type_assessment", assess_jobs)

        # Combine in date order so the same-actor rule sees earlier finals from this run too.
        verify, prelim = [], {}
        for i in items:
            out, aid = assessed.get(i.id, (None, None))
            if not out:
                continue
            with db.connect() as c:
                finals = data.same_actor_finals(c, i, sorted(allowed[i.id]))
            dims, raw, conf = combine(self.rubric, i, out["dimensions"], allowed[i.id], finals)
            if raw is None:
                continue
            prelim[i.id] = (dims, raw, conf, aid)
            self._write_score(i, aid, dims, raw, conf)
            if raw < VERIFY_IF_RAW_BELOW or any(0 <= d["final"] <= VERIFY_IF_FINAL_AT_MOST for d in dims.values()):
                verify.append(i)
        if not verify:
            return
        vout = self.call("verification", [
            (i.id, i.as_of, *prompts.verification(self.rubric, i, assess_users[i.id], assessed[i.id][0]))
            for i in verify])
        for i in verify:
            second, vid = vout.get(i.id, (None, None))
            if not second:
                continue
            dims, raw, conf, _ = prelim[i.id]
            with db.connect() as c:
                finals = data.same_actor_finals(c, i, sorted(allowed[i.id]))
            dims2, _, _ = combine(self.rubric, i, second["dimensions"], allowed[i.id], finals)
            dims, raw, conf = merge(dims, dims2, conf)
            self._write_score(i, vid, dims, raw, conf)

    def _second_check(self, checks, check_meta, material) -> None:
        """Flags need two passes that agree. Re-check bad verdicts on the next tier up."""
        bad = [s for s, (o, _) in checks.items() if o and o.get("verdict") in BAD_VERDICTS]
        if not bad:
            return
        saved = self.stages["citation_check"]
        self.stages["citation_check"] = "sonnet" if saved == "haiku" else "opus"
        try:
            again = self.call("citation_check", [
                (s, check_meta[s][0].as_of,
                 prompts.citation_check(check_meta[s][0], check_meta[s][1], material(check_meta[s][1]))[0]
                 + "\n\n(Second, independent check.)",
                 prompts.citation_check(check_meta[s][0], check_meta[s][1], "")[1]) for s in bad])
        finally:
            self.stages["citation_check"] = saved
        with db.connect() as c:
            for s in bad:
                first, first_id = checks[s]
                second, second_id = again.get(s, (None, None))
                ct = check_meta[s][1]
                if ct["citation_id"] and first_id:
                    c.execute("INSERT INTO citation_check (citation_id, analysis_id, verdict) VALUES (%s, %s, %s)"
                              " ON CONFLICT (citation_id) DO UPDATE SET analysis_id = EXCLUDED.analysis_id,"
                              " verdict = EXCLUDED.verdict", (ct["citation_id"], first_id, first["verdict"]))
                if second and second.get("verdict") in BAD_VERDICTS and ct["citation_id"] and not self.compare:
                    c.execute("INSERT INTO flag (citation_id, kind, severity, evidence, verified_by)"
                              " SELECT %s, 'substantive', 2, %s, %s WHERE NOT EXISTS"
                              " (SELECT 1 FROM flag WHERE citation_id = %s AND verified_by = %s)",
                              (ct["citation_id"], Jsonb([first["reason"], second["reason"]]), second_id,
                               ct["citation_id"], second_id))

    def _write_presorted(self, items) -> None:
        with db.connect() as c:
            for i in items:
                dims = {k: {"rating": 4, "rating_independent": 4, "final": 4, "evidence": [],
                            "reason": f"Routine template: {i.presort}"}
                        for k in self.rubric["types"][i.type]["dimensions"]}
                aid = c.execute(
                    "INSERT INTO analysis (kind, subject_id, as_of, input_hash, rubric_version, model, output)"
                    " VALUES ('score', %s, %s, %s, %s, 'rule', %s)"
                    " ON CONFLICT (kind, input_hash) DO UPDATE SET output = EXCLUDED.output RETURNING id",
                    (i.id, i.as_of, _hash("presort", self.rv, i.id, i.presort), self.rv,
                     Jsonb({"presort": i.presort}))).fetchone()[0]
                self._write_score(i, aid, dims, 1.0, 1.0, c)
        if items:
            self.log(f"pre-sorted routine: {len(items)} (no model calls)")

    def _write_score(self, item, analysis_id, dims, raw, conf, c=None) -> None:
        if self.compare:
            current = False
        else:
            current = True
        own = c is None
        c = c or db.connect()
        try:
            if current:
                c.execute("UPDATE score SET current = false WHERE artifact_id = %s AND current", (item.id,))
            c.execute("INSERT INTO score (artifact_id, analysis_id, dimensions, raw, confidence, current)"
                      " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (artifact_id, analysis_id) DO UPDATE SET"
                      " dimensions = EXCLUDED.dimensions, raw = EXCLUDED.raw, confidence = EXCLUDED.confidence,"
                      " current = EXCLUDED.current",
                      (item.id, analysis_id, Jsonb(dims), raw, conf, current))
            if own:
                c.commit()
        finally:
            if own:
                c.close()


def combine(rubric: dict, item, out_dims: dict, allowed: set[str], same_actor: dict[str, dict]):
    """Apply evidence filtering and the same-actor rule. Returns (dims, raw 0-1, confidence)."""
    dims = {}
    for name in rubric["types"][item.type]["dimensions"]:
        d = out_dims.get(name) or {}
        rating = d.get("rating", prompts.INSUFFICIENT)
        independent = d.get("rating_independent", prompts.INSUFFICIENT)
        evidence = [e for e in d.get("evidence", []) if e in allowed]   # drops invented or later ids
        if rating >= 0 and not evidence:
            rating = independent = prompts.INSUFFICIENT
        relied = [same_actor[e][name]["final"] for e in evidence
                  if e in same_actor and name in same_actor[e] and same_actor[e][name].get("final", -1) >= 0]
        final = rating if rating < 0 else final_rating(rating, independent if independent >= 0 else None, relied)
        dims[name] = {"rating": rating, "rating_independent": independent, "final": final,
                      "evidence": evidence, "reason": d.get("reason", ""), "same_actor_cap": bool(relied)}
    valid = [d["final"] for d in dims.values() if d["final"] >= 0]
    if not valid:
        return dims, None, 0.0
    return dims, sum(valid) / len(valid) / 4, len(valid) / len(dims)


def merge(first: dict, second: dict, confidence: float):
    """Two independent passes: average each dimension (rounding toward less normal) and
    lower confidence when they differ by two or more points."""
    dims, disagree = {}, False
    for name, a in first.items():
        b = second.get(name, a)
        if a["final"] < 0 or b["final"] < 0:
            final = max(a["final"], b["final"])
        else:
            disagree |= abs(a["final"] - b["final"]) >= DISAGREEMENT
            final = floor((a["final"] + b["final"]) / 2)
        dims[name] = {**a, "final": final, "second_pass": b["final"]}
    valid = [d["final"] for d in dims.values() if d["final"] >= 0]
    raw = sum(valid) / len(valid) / 4 if valid else 0.0
    conf = len(valid) / len(dims) * (DISAGREEMENT_CONFIDENCE if disagree else 1.0)
    return dims, raw, min(confidence, conf) if disagree else conf

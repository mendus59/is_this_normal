"""The paid scoring pipeline. Runs only under `itn analyze` without --dry-run, in the
`paid` GitHub environment. Every model output is cached in `analysis` by a hash of its
inputs, so a re-run (or a run stopped by its budget) never pays twice for the same call.

Stages, each sent as one batch across all items:
  summary -> authority_digest -> citation_check -> type_assessment (which also reads the
  comparable earlier items) -> combine (same-actor rule, free) -> verification for low
  scores -> final score. In --compare runs with compare_escalate, low Haiku assessments
  are re-run on the escalation tier before combining.
"""

import hashlib
import json
import os
import time
from math import floor

from psycopg.types.json import Jsonb

from itn import db
from itn.analyze import data, prompts
from itn.analyze.actors import final_rating
from itn.analyze.estimate import EFFORT
from itn.analyze import llm
from itn.analyze.llm import BudgetExceeded, Req, Spend, run as run_requests, tier_of

MAX_DIGESTS_PER_ITEM = 15
MAX_CHECKS_PER_ITEM = 60
BAD_VERDICTS = {"contradicts", "mischaracterized"}
VERIFY_IF_FINAL_AT_MOST = 1
VERIFY_IF_RAW_BELOW = 0.5
DISAGREEMENT = 2
DISAGREEMENT_CONFIDENCE = 0.7
# Small stages skip the Batch API: its queue can take hours, which costs more in waiting
# than the discount saves on a few dozen calls.
BATCH_MIN = 100
# GitHub stops the Analyze job at 360 minutes; stop waiting on batches before that.
WAIT_MINUTES = float(os.environ.get("ITN_WAIT_MINUTES", "320"))


class BatchesPending(RuntimeError):
    pass


INPUT_SLICE = 100


def _with_retry(fn, part, tries: int = 3):
    """Run fn(conn, part) on a fresh connection, retrying when the server drops it."""
    import psycopg
    for attempt in range(tries):
        try:
            with db.connect() as c:
                fn(c, part)
            return
        except psycopg.OperationalError:
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Pipeline:
    def __init__(self, r2, client, rubric: dict, budget: float, max_tier: str, compare: bool = False,
                 dispatched_by: str | None = None, scope: dict | None = None, log=print,
                 profile: str = "standard", record_run: bool = True):
        self.r2, self.client, self.rubric, self.log = r2, client, rubric, log
        self.compare = compare
        self.economy = rubric.get("economy") if profile == "economy" else None
        if profile == "economy" and not self.economy:
            raise ValueError("the rubric has no economy settings")
        self.max_tier = max_tier
        self.effort = {**EFFORT, **(rubric.get("effort") or {})}
        if self.economy:
            # The economy profile is its own setting; --compare only keeps its scores off the site.
            self.stages = dict(self.economy["stages"])
            self.effort.update(self.economy.get("effort") or {})
            self.escalate = {}
        else:
            self.stages = {**rubric["stages"], **(rubric.get("compare_stages", {}) if compare else {})}
            self.escalate = (rubric.get("compare_escalate") or {}) if compare else {}
        self.models = rubric["models"]
        self.rv = f"{rubric['version']}{'-economy' if self.economy else ''}{'-compare' if compare else ''}"
        run_cfg = rubric.get("run", {})
        self.batch, self.caching = bool(run_cfg.get("batch")), bool(run_cfg.get("prompt_caching"))
        self.spend = Spend(budget)
        self.reserved, self._rough = 0.0, {}
        self.deadline = time.monotonic() + WAIT_MINUTES * 60
        self.run_id = None
        if not record_run:     # free uses (review packets) call no model and log no run
            return
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
        keyed = {s: (a, u, sch, _hash(stage, self.rv, self.models[tier], self.effort.get(stage), system, u)) for s, a, u, sch in jobs}
        out: dict[str, tuple[dict | None, int | None]] = {}
        by_hash = {k[3]: s for s, k in keyed.items()}
        with db.connect() as c:
            for aid, h, output in c.execute(
                    "SELECT id, input_hash, output FROM analysis WHERE kind = %s AND input_hash = ANY(%s)",
                    (stage, list(by_hash))).fetchall():
                out[by_hash[h]] = (output, aid)
        todo = [s for s in keyed if s not in out]
        if not todo:
            return out
        open_batches = self._open_batches(stage) if self.batch else {}
        inflight = {v["hash"] for m in open_batches.values() for v in m.values()}
        remaining = [s for s in todo if keyed[s][3] not in inflight]
        self.log(f"{stage}: {len(remaining)} calls on {tier} ({len(out)} cached,"
                 f" {len(todo) - len(remaining)} already sent in earlier batches)")
        if remaining and not (self.batch and len(remaining) >= BATCH_MIN):
            reqs = [Req(f"{stage[:8]}-{i}", stage, tier, system, keyed[s][1], keyed[s][2], self.effort.get(stage))
                    for i, s in enumerate(remaining)]
            subject = {r.custom_id: s for r, s in zip(reqs, remaining)}
            results = run_requests(self.client, reqs, self.models, self.spend, False, self.caching)
            with db.connect() as c:
                for cid, res in results.items():
                    out[subject[cid]] = self._store(c, stage, subject[cid], keyed[subject[cid]][0],
                                                    keyed[subject[cid]][3], res)
                self._save_run(c, "running")
            remaining = []
        for k in range(0, len(remaining), llm.CHUNK):
            chunk = remaining[k:k + llm.CHUNK]
            reqs = [Req(f"r{n}", stage, tier, system, keyed[s][1], keyed[s][2], self.effort.get(stage))
                    for n, s in enumerate(chunk)]
            rough = sum(llm.rough_cost(r, True) for r in reqs)
            self.spend.check(self.reserved + rough)
            bid = llm.submit_batch(self.client, reqs, self.models, self.caching)
            mapping = {r.custom_id: {"subject": s, "as_of": str(keyed[s][0]), "hash": keyed[s][3], "tier": tier}
                       for r, s in zip(reqs, chunk)}
            with db.connect() as c:
                c.execute("INSERT INTO model_batch (id, stage, run_id, requests) VALUES (%s, %s, %s, %s)",
                          (bid, stage, self.run_id, Jsonb(mapping)))
                c.commit()
            open_batches[bid] = mapping
            self.reserved += rough
            self._rough[bid] = rough
        if open_batches:
            self._collect(stage, open_batches, {keyed[s][3]: s for s in todo}, out)
        return out

    def _open_batches(self, stage: str) -> dict[str, dict]:
        with db.connect() as c:
            return {bid: m for bid, m in c.execute(
                "SELECT id, requests FROM model_batch WHERE stage = %s AND status = 'submitted'"
                " AND created_at > now() - interval '28 days'", (stage,)).fetchall()}

    def _collect(self, stage: str, batches: dict[str, dict], wanted: dict[str, str], out: dict) -> None:
        """Wait for batches and store every result, including ones sent by earlier runs.
        At the deadline, stop cleanly: the next run collects what is still queued."""
        self.log(f"  waiting on {len(batches)} batches: {', '.join(sorted(batches))}")
        pending = set(batches)
        while pending:
            for bid in sorted(pending):
                if not llm.batch_ended(self.client, bid):
                    continue
                mapping = batches[bid]
                results = llm.collect_batch(self.client, bid, {cid: v["tier"] for cid, v in mapping.items()},
                                            self.models, self.spend)
                with db.connect() as c:
                    for cid, res in results.items():
                        v = mapping[cid]
                        got = self._store(c, stage, v["subject"], v["as_of"], v["hash"], res)
                        if v["hash"] in wanted:
                            out[wanted[v["hash"]]] = got
                    c.execute("UPDATE model_batch SET status = 'collected', collected_at = now() WHERE id = %s", (bid,))
                    self._save_run(c, "running")
                    c.commit()
                self.reserved -= self._rough.pop(bid, 0.0)
                pending.discard(bid)
                self.log(f"  collected {bid} ({len(results)} results), ${self.spend.spent:.2f} spent")
            if pending:
                if time.monotonic() > self.deadline:
                    raise BatchesPending(f"{len(pending)} {stage} batches still in Anthropic's queue;"
                                         " the next run collects them without paying again")
                time.sleep(llm.POLL_SECONDS)

    def _store(self, c, stage: str, subject: str, as_of, input_hash: str, res) -> tuple[dict | None, int | None]:
        if res.data is None:
            self.log(f"  {stage} {subject}: {res.error}")
            return None, None
        row = c.execute(
            "INSERT INTO analysis (kind, subject_id, as_of, input_hash, rubric_version, model, output,"
            " cost_usd, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (kind, input_hash) DO UPDATE SET output = EXCLUDED.output RETURNING id",
            (stage, subject, as_of, input_hash, self.rv, res.model, Jsonb(res.data),
             round(res.cost_usd, 4), self.run_id)).fetchone()
        return res.data, row[0]

    # ---- the pipeline -------------------------------------------------------------

    def score(self, ids: list[str]) -> None:
        status = "done"
        try:
            self._score(ids)
        except BudgetExceeded as e:
            self.log(str(e))
            status = "stopped_budget"
        except BatchesPending as e:
            self.log(str(e))
            status = "waiting_on_batches"
        with db.connect() as c:
            self._save_run(c, status)
        self.log(f"Run {self.run_id} {status}: ${self.spend.spent:.2f} of ${self.spend.budget:.2f}")

    def _score(self, ids: list[str]) -> None:
        with db.connect() as c:
            items = data.load_items(c, ids)
        # Trial runs write them too (off the site) so calibration counts them.
        self._write_presorted([i for i in items if i.presort])
        items = [i for i in items if not i.presort]
        if not items:
            return
        if self.economy:
            return self._score_economy(items)
        texts = {i.id: data.text_of(self.r2, i.text_keys, data.TEXT_MAX_CHARS) for i in items}
        full = {i.id: texts[i.id] for i in items if i.type == "scotus_case"}
        with db.connect() as c:
            cites = {i.id: data.citations(c, self.r2, i, texts[i.id]) for i in items}
            candidates = {i.id: data.analog_candidates(c, i) for i in items}
            facts = {i.id: data.record_facts(c, i, full.get(i.id), [ct["to"] for ct in cites[i.id]]) for i in items}

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

        assess_users, assess_jobs, allowed = {}, [], {}
        for i in items:
            top = cites[i.id][:MAX_DIGESTS_PER_ITEM]
            dg = {ct["to"]: material(ct) for ct in top}
            ck = {s.split("->", 1)[1]: (checks.get(s, (None,))[0] or {}).get("verdict")
                  for s in check_meta if s.startswith(i.id + "->")}
            user, schema = prompts.type_assessment(self.rubric, i, summary[i.id], dg, ck, candidates[i.id],
                                                   facts[i.id])
            assess_users[i.id] = user
            allowed[i.id] = {i.id} | {ct["to"] for ct in cites[i.id]} | {cd["id"] for cd in candidates[i.id]}
            assess_jobs.append((i.id, i.as_of, user, schema))
        assessed = self.call("type_assessment", assess_jobs)
        if self.escalate:
            assessed = self._escalate(items, assessed, assess_jobs, allowed)

        # Combine in date order so the same-actor rule sees earlier finals from this run too.
        verify, prelim = [], {}
        for i in items:
            out, aid = assessed.get(i.id, (None, None))
            if not out:
                continue
            with db.connect() as c:
                finals = data.same_actor_finals(c, i, sorted(allowed[i.id]))
            dims, raw, conf = combine(self.rubric, i, out["dimensions"], allowed[i.id], finals)
            dims, raw, conf = apply_rules(self.rubric, i, dims, raw, conf, facts[i.id])
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
            dims, raw, conf = merge(dims, dims2, conf, cap_of(self.rubric))
            dims, raw, conf = apply_rules(self.rubric, i, dims, raw, conf, facts[i.id])
            self._write_score(i, vid, dims, raw, conf)

    def economy_inputs(self, items) -> dict:
        """Everything the economy assessment sees, built only from free material dated on or
        before each item's as-of date. Shared by paid runs and session review packets."""
        e = self.economy
        texts = {i.id: data.text_of(self.r2, i.text_keys, e["item_chars"]) for i in items}
        # Rulings: scan the whole controlling opinion (free) for overruling language.
        full = {i.id: data.text_of(self.r2, i.text_keys, data.TEXT_MAX_CHARS) for i in items if i.type == "scotus_case"}
        cites, candidates, facts = {}, {}, {}

        def gather(c, part):
            for i in part:
                cites[i.id] = data.citations(c, self.r2, i, texts[i.id])
                candidates[i.id] = [{**cd, "summary": cd["summary"][:e["analog_chars"]]}
                                    for cd in data.analog_candidates(c, i, e["analogs"])]
                facts[i.id] = data.record_facts(c, i, full.get(i.id), [ct["to"] for ct in cites[i.id]])

        # A fresh connection per slice: one connection held through thousands of queries
        # was dropped by the server an hour into the first full run.
        for k in range(0, len(items), INPUT_SLICE):
            _with_retry(gather, items[k:k + INPUT_SLICE])
        wanted = {ct["to"] for i in items for ct in cites[i.id][:e["max_cited"]] if not ct["summary"]}
        keys = {}
        wanted_list = sorted(wanted)
        for k in range(0, len(wanted_list), INPUT_SLICE * 5):
            _with_retry(lambda c, part: keys.update({a: data._text_keys(c, a) for a in part}),
                        wanted_list[k:k + INPUT_SLICE * 5])
        free = {a: data.text_of(self.r2, k, e["cited_chars"]) for a, k in keys.items() if k}

        def material(ct):
            return (ct["summary"] or free.get(ct["to"]) or ct["title"])[:e["cited_chars"]]

        def item_material(i):
            official = (i.summary or "")[:e["official_summary_chars"]]
            start = texts[i.id][: max(0, e["item_chars"] - len(official))]
            return f"{official}\n\nText (start):\n{start}" if official else f"Text (start):\n{start}"

        users, schemas, allowed = {}, {}, {}
        for i in items:
            top = cites[i.id][:e["max_cited"]]
            users[i.id], schemas[i.id] = prompts.type_assessment(
                self.rubric, i, item_material(i), {ct["to"]: material(ct) for ct in top}, {},
                candidates[i.id], facts[i.id])
            allowed[i.id] = {i.id} | {ct["to"] for ct in cites[i.id]} | {cd["id"] for cd in candidates[i.id]}
        return {"users": users, "schemas": schemas, "allowed": allowed, "facts": facts, "cites": cites,
                "material": material}

    def _score_economy(self, items) -> None:
        """One Haiku assessment per item from free material (official summary or syllabus,
        the start of the text, the free summaries of what it cites). Items rated unusual get
        citation checks and a second pass that sees the check verdicts."""
        inp = self.economy_inputs(items)
        users, allowed, facts, cites, material = inp["users"], inp["allowed"], inp["facts"], inp["cites"], inp["material"]
        e = self.economy
        jobs = [(i.id, i.as_of, users[i.id], inp["schemas"][i.id]) for i in items]
        assessed = self.call("type_assessment", jobs)

        unusual, prelim = [], {}
        for i in items:
            out, aid = assessed.get(i.id, (None, None))
            if not out:
                continue
            with db.connect() as c:
                finals = data.same_actor_finals(c, i, sorted(allowed[i.id]))
            dims, raw, conf = combine(self.rubric, i, out["dimensions"], allowed[i.id], finals)
            dims, raw, conf = apply_rules(self.rubric, i, dims, raw, conf, facts[i.id])
            if raw is None:
                continue
            prelim[i.id] = (dims, raw, conf)
            self._write_score(i, aid, dims, raw, conf)
            if raw < VERIFY_IF_RAW_BELOW or any(0 <= d["final"] <= VERIFY_IF_FINAL_AT_MOST for d in dims.values()):
                unusual.append(i)
        if not unusual:
            return

        check_jobs, check_meta = [], {}
        for i in unusual:
            for ct in [ct for ct in cites[i.id] if ct["context"]][:e["max_checks"]]:
                sid = f"{i.id}->{ct['to']}"
                check_meta[sid] = (i, ct)
                check_jobs.append((sid, i.as_of, *prompts.citation_check(i, ct, material(ct))))
        checks = self.call("citation_check", check_jobs)
        self._second_check(checks, check_meta, material, tier="haiku")

        vjobs = []
        for i in unusual:
            ck = {s.split("->", 1)[1]: (checks.get(s, (None,))[0] or {}).get("verdict")
                  for s in check_meta if s.startswith(i.id + "->")}
            user = users[i.id] + (f"\n\nCitation checks:\n{json.dumps(ck, indent=1)}" if ck else "")
            vjobs.append((i.id, i.as_of, *prompts.verification(self.rubric, i, user, assessed[i.id][0])))
        vout = self.call("verification", vjobs)
        vjobs_by = {j[0]: j for j in vjobs}
        hard = []
        for i in unusual:
            second, vid = vout.get(i.id, (None, None))
            if not second:
                continue
            dims, raw, conf = prelim[i.id]
            with db.connect() as c:
                finals = data.same_actor_finals(c, i, sorted(allowed[i.id]))
            dims2, _, _ = combine(self.rubric, i, second["dimensions"], allowed[i.id], finals)
            dims, raw, conf = merge(dims, dims2, conf, cap_of(self.rubric))
            dims, raw, conf = apply_rules(self.rubric, i, dims, raw, conf, facts[i.id])
            self._write_score(i, vid, dims, raw, conf)
            first = prelim[i.id][0]
            if any(first[k]["final"] >= 0 and dims2[k]["final"] >= 0
                   and abs(first[k]["final"] - dims2[k]["final"]) >= DISAGREEMENT for k in first if k in dims2):
                hard.append((i, vjobs_by[i.id]))
        self._settle_hard(hard, allowed, facts)

    def _settle_hard(self, hard, allowed, facts) -> None:
        """Economy profile with `escalate`: items whose two Haiku passes disagreed by two or
        more points on a dimension are re-assessed once on the stronger tier, which settles
        them (its rating replaces the merged one)."""
        esc = (self.economy or {}).get("escalate") or {}
        if not hard or not esc.get("to"):
            if hard:
                self.log(f"{len(hard)} items had passes that disagreed by {DISAGREEMENT}+ (no escalation set)")
            return
        saved = self.stages["verification"]
        self.stages["verification"] = esc["to"]
        try:
            out = self.call("verification", [j for _, j in hard])
        finally:
            self.stages["verification"] = saved
        for i, _ in hard:
            third, tid = out.get(i.id, (None, None))
            if not third:
                continue
            with db.connect() as c:
                finals = data.same_actor_finals(c, i, sorted(allowed[i.id]))
            dims, raw, conf = combine(self.rubric, i, third["dimensions"], allowed[i.id], finals)
            if raw is None:
                continue
            dims, raw, conf = apply_rules(self.rubric, i, dims, raw, conf, facts[i.id])
            self._write_score(i, tid, dims, raw, conf * DISAGREEMENT_CONFIDENCE)
        self.log(f"settled {len(hard)} disputed items on {esc['to']}")

    def _escalate(self, items, assessed, assess_jobs, allowed):
        """Comparison runs: re-assess low first-pass scores on the escalation tier."""
        low = set()
        for i in items:
            out = assessed.get(i.id, (None, None))[0]
            if not out:
                continue
            dims, raw, _ = combine(self.rubric, i, out["dimensions"], allowed[i.id], {})
            if raw is not None and (raw < self.escalate.get("if_raw_below", 0)
                                    or any(0 <= d["final"] <= self.escalate.get("if_any_final_at_most", -1)
                                           for d in dims.values())):
                low.add(i.id)
        if not low:
            return assessed
        saved = self.stages["type_assessment"]
        self.stages["type_assessment"] = self.escalate["to"]
        try:
            again = self.call("type_assessment", [j for j in assess_jobs if j[0] in low])
        finally:
            self.stages["type_assessment"] = saved
        self.log(f"escalated {len(low)} of {len(assessed)} assessments to {self.escalate['to']}")
        return {**assessed, **{k: v for k, v in again.items() if v[0]}}

    def _second_check(self, checks, check_meta, material, tier: str | None = None) -> None:
        """Flags need two passes that agree. Re-check bad verdicts on the next tier up
        (or on `tier`, for the economy profile)."""
        bad = [s for s, (o, _) in checks.items() if o and o.get("verdict") in BAD_VERDICTS]
        if not bad:
            return
        saved = self.stages["citation_check"]
        self.stages["citation_check"] = tier or ("sonnet" if saved == "haiku" else "opus")
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
    return dims, overall(valid, cap_of(rubric)), len(valid) / len(dims)


def apply_rules(rubric: dict, item, dims: dict, raw, conf, facts: dict):
    """Fixed rules that replace a model judgment when the record settles it. Each rule
    names its evidence. Today: a ruling whose controlling opinion expressly overrules
    one of the Court's cases gets the rubric's rating on precedent."""
    rules = rubric.get("rules") or {}
    rating = rules.get("express_overruling_precedent")
    passages = (facts or {}).get("expressly_overrules")
    if item.type == "scotus_case" and rating is not None and passages and "precedent" in dims:
        d = dims["precedent"]
        if d["final"] < 0 or d["final"] > rating:
            dims["precedent"] = {**d, "rating": rating, "rating_independent": rating, "final": rating,
                                 "evidence": sorted(set(d.get("evidence") or []) | {item.id}),
                                 "reason": f"Rule: the controlling opinion expressly overrules: {passages[0][:300]}",
                                 "rule": "express_overruling"}
            valid = [x["final"] for x in dims.values() if x["final"] >= 0]
            raw = overall(valid, cap_of(rubric))
            conf = max(conf, len(valid) / len(dims))
    return dims, raw, conf


def cap_of(rubric: dict) -> int | None:
    return (rubric.get("combine") or {}).get("max_bands_above_worst")


def overall(finals: list[int], cap: int | None = None) -> float:
    """Equal-weight mean on 0-1. With `cap`, the result's band is at most `cap` bands
    above the worst dimension's band (bands are 20 points wide, rating r sits in band r)."""
    raw = sum(finals) / len(finals) / 4
    if cap is not None:
        ceiling = (min(finals) + cap + 1) * 0.2 - 0.001
        if ceiling < 1:
            raw = min(raw, ceiling)
    return raw


def merge(first: dict, second: dict, confidence: float, cap: int | None = None):
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
    raw = overall(valid, cap) if valid else 0.0
    conf = len(valid) / len(dims) * (DISAGREEMENT_CONFIDENCE if disagree else 1.0)
    return dims, raw, min(confidence, conf) if disagree else conf

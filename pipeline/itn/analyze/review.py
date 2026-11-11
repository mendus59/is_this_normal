"""Session review: a stronger model re-rates selected items outside the paid pipeline.

`export` (free) writes review packets: for each selected item, the exact material the
economy assessment saw (built by the data layer, so nothing after its as-of date), the
current economy rating as the first analyst, and the answer schema. A reviewer working in
a Claude session answers each packet with JSON in that schema. `import` (free) checks
each answer against its packet and writes it as the item's current score. Neither step
calls a model API or needs ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from psycopg.types.json import Jsonb

from itn import db
from itn.analyze import data, prompts
from itn.analyze.stages import Pipeline, _hash, apply_rules, cap_of, combine, merge

REVIEW_SUFFIX = "-review"
CHUNK = 25


def select(conn, max_raw: float, min_confidence: float, types: list[str] | None, limit: int,
           reviewer: str) -> list[str]:
    """Current economy scores that are least normal or least certain, least normal first,
    skipping items this reviewer already settled."""
    rows = conn.execute(
        "SELECT s.artifact_id FROM score s JOIN analysis an ON an.id = s.analysis_id"
        " JOIN artifact a ON a.id = s.artifact_id"
        " WHERE s.current AND an.rubric_version LIKE %s AND an.model <> 'rule'"
        " AND (s.raw < %s OR s.confidence < %s) AND (%s::text[] IS NULL OR a.type = ANY(%s))"
        " AND NOT EXISTS (SELECT 1 FROM analysis r WHERE r.subject_id = s.artifact_id"
        "                 AND r.rubric_version LIKE %s AND r.model = %s)"
        " ORDER BY s.raw, s.confidence LIMIT %s",
        ("%-economy", max_raw, min_confidence, types, types, f"%{REVIEW_SUFFIX}", reviewer, limit)).fetchall()
    return [r[0] for r in rows]


def _first(dims: dict) -> dict:
    return {"dimensions": {k: {x: d.get(x) for x in ("rating", "rating_independent", "evidence", "reason")}
                           for k, d in dims.items()}}


def export(r2, rubric: dict, ids: list[str], out: Path, log=print) -> int:
    pipe = Pipeline(r2, None, rubric, 0, "haiku", profile="economy", record_run=False)
    system = prompts.system_prompt(rubric, "verification")
    n = 0
    with out.open("w") as f:
        for start in range(0, len(ids), CHUNK):
            with db.connect() as c:
                items = data.load_items(c, ids[start:start + CHUNK])
                current = {r[0]: (r[1], r[2], r[3]) for r in c.execute(
                    "SELECT artifact_id, dimensions, raw, confidence FROM score WHERE current AND artifact_id = ANY(%s)",
                    ([i.id for i in items],)).fetchall()}
            inp = pipe.economy_inputs(items)
            for i in items:
                dims, raw, conf = current.get(i.id, ({}, None, None))
                user, schema = prompts.verification(rubric, i, inp["users"][i.id], _first(dims))
                f.write(json.dumps({
                    "id": i.id, "type": i.type, "as_of": i.as_of.isoformat(), "title": i.title,
                    "current": {"raw": raw, "confidence": conf},
                    "allowed": sorted(inp["allowed"][i.id]), "facts": inp["facts"][i.id],
                    "system": system, "prompt": user, "schema": schema,
                    "prompt_hash": _hash(system, user)}, default=str) + "\n")
                n += 1
            log(f"  {min(start + CHUNK, len(ids))} of {len(ids)} packets")
    return n


def _facts(facts: dict, answer: dict | None = None) -> dict:
    """Packet facts, with the express-overruling sentences re-checked by the current rule so
    a packet exported before a rule fix does not carry a false positive into the score. The
    reviewer read those sentences: a reviewed precedent rating above 0 means the reviewer
    judged that none of them is the Court overruling a precedent, and that judgment stands."""
    express = facts.get("expressly_overrules")
    if isinstance(express, list):
        kept = [s for s in express if data.express_overrulings(s)]
        if (((answer or {}).get("precedent") or {}).get("rating") or 0) > 0:
            kept = []
        return {**facts, "expressly_overrules": kept or False}
    return facts


def _valid(answer: dict, packet: dict, rubric: dict) -> str | None:
    dims = rubric["types"][packet["type"]]["dimensions"]
    got = answer.get("dimensions") or {}
    if set(got) != set(dims):
        return f"dimensions {sorted(got)} != {sorted(dims)}"
    for k, d in got.items():
        for x in ("rating", "rating_independent"):
            if d.get(x) not in (prompts.INSUFFICIENT, 0, 1, 2, 3, 4):
                return f"{k}.{x} = {d.get(x)!r}"
        if not isinstance(d.get("evidence"), list) or not isinstance(d.get("reason"), str):
            return f"{k}: evidence must be a list and reason a string"
    return None


def import_answers(rubric: dict, packets: list[Path], answers: list[Path], reviewer: str, log=print) -> int:
    """Each answer line: {"id", "prompt_hash", "dimensions": {...}, "note"}. The prompt hash
    must match its packet, so an answer is always tied to the material it was given."""
    by_id = {}
    for p in packets:
        for line in p.read_text().splitlines():
            if line.strip():
                pk = json.loads(line)
                by_id[pk["id"]] = pk
    rv = f"{rubric['version']}-economy{REVIEW_SUFFIX}"
    written = 0
    pipe = Pipeline(None, None, rubric, 0, "haiku", profile="economy", record_run=False)
    with db.connect() as c:
        for a_path in answers:
            for n, line in enumerate(a_path.read_text().splitlines(), 1):
                if not line.strip():
                    continue
                ans = json.loads(line)
                pk = by_id.get(ans.get("id"))
                where = f"{a_path.name}:{n} {ans.get('id')}"
                if not pk:
                    log(f"  skip {where}: no packet")
                    continue
                if ans.get("prompt_hash") != pk["prompt_hash"]:
                    log(f"  skip {where}: prompt_hash does not match its packet")
                    continue
                if err := _valid(ans, pk, rubric):
                    log(f"  skip {where}: {err}")
                    continue
                item = data.load_items(c, [pk["id"]])[0]
                if item.as_of.isoformat() != pk["as_of"]:
                    log(f"  skip {where}: as-of date changed since export")
                    continue
                allowed = set(pk["allowed"])
                finals = data.same_actor_finals(c, item, sorted(allowed))
                dims, raw, conf = combine(rubric, item, ans["dimensions"], allowed, finals)
                if raw is None:
                    log(f"  skip {where}: no rating had evidence")
                    continue
                dims, raw, conf = apply_rules(rubric, item, dims, raw, conf, _facts(pk["facts"], ans["dimensions"]))
                output = {"dimensions": ans["dimensions"], "note": ans.get("note", ""),
                          "reviewed": dt.date.today().isoformat()}
                aid = c.execute(
                    "INSERT INTO analysis (kind, subject_id, as_of, input_hash, rubric_version, model, output)"
                    " VALUES ('verification', %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (kind, input_hash) DO UPDATE SET output = EXCLUDED.output RETURNING id",
                    (item.id, item.as_of, _hash("review", rv, reviewer, pk["prompt_hash"]), rv, reviewer,
                     Jsonb(output))).fetchone()[0]
                pipe._write_score(item, aid, dims, raw, conf, c)
                written += 1
        c.commit()
    log(f"Imported {written} reviewed scores")
    return written


def rescore_rules(r2, rubric: dict, log=print) -> tuple[int, int]:
    """Free: re-check stored economy scores whose precedent rating was set by the
    express-overruling rule. The facts are rebuilt with the current rule; where it no longer
    fires, the score is rebuilt from the model output already stored (and paid for), exactly
    as the run combined it. Returns (rescored, still set by the rule)."""
    rv = f"{rubric['version']}-economy"
    with db.connect() as c:
        rows = c.execute(
            "SELECT s.artifact_id, s.analysis_id, an.kind FROM score s JOIN analysis an ON an.id = s.analysis_id"
            " WHERE s.current AND an.rubric_version = %s"
            " AND s.dimensions->'precedent'->>'rule' = 'express_overruling' ORDER BY s.artifact_id", (rv,)).fetchall()
    log(f"{len(rows)} current scores have precedent set by the express-overruling rule")
    pipe = Pipeline(r2, None, rubric, 0, "haiku", profile="economy", record_run=False)
    changed = kept = 0
    for start in range(0, len(rows), CHUNK):
        part = rows[start:start + CHUNK]
        with db.connect() as c:
            items = {i.id: i for i in data.load_items(c, [r[0] for r in part])}
        inp = pipe.economy_inputs(list(items.values()))
        with db.connect() as c:
            for artifact_id, analysis_id, kind in part:
                item = items.get(artifact_id)
                if item is None:
                    continue
                if inp["facts"][item.id].get("expressly_overrules"):
                    kept += 1
                    continue
                allowed = set(inp["allowed"][item.id])
                finals = data.same_actor_finals(c, item, sorted(allowed))
                out = c.execute("SELECT output FROM analysis WHERE id = %s", (analysis_id,)).fetchone()[0]
                dims, raw, conf = combine(rubric, item, out["dimensions"], allowed, finals)
                if kind == "verification":
                    first = c.execute(
                        "SELECT output FROM analysis WHERE kind = 'type_assessment' AND subject_id = %s"
                        " AND rubric_version = %s ORDER BY id DESC LIMIT 1", (item.id, rv)).fetchone()
                    if first:
                        d1, r1, c1 = combine(rubric, item, first[0]["dimensions"], allowed, finals)
                        if r1 is not None:
                            dims, raw, conf = merge(d1, dims, c1, cap_of(rubric))
                if raw is None:
                    continue
                c.execute("UPDATE score SET dimensions = %s, raw = %s, confidence = %s"
                          " WHERE artifact_id = %s AND analysis_id = %s",
                          (Jsonb(dims), raw, conf, item.id, analysis_id))
                changed += 1
            c.commit()
        log(f"  {min(start + CHUNK, len(rows))} of {len(rows)} checked")
    log(f"Rescored {changed}; {kept} still expressly overrule")
    return changed, kept

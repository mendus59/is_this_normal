"""Command line entry point: `itn <command>`."""

import argparse
import os
import sys

from itn import db


def cmd_migrate(args) -> None:
    with db.connect() as conn:
        applied = db.migrate(conn)
    print("Applied:", ", ".join(applied) if applied else "nothing (up to date)")


def cmd_ingest(args) -> None:
    from itn.ingest import (
        cap_scotus, cited_scotus, cited_usc, congress_laws, courtlistener_scotus, federal_register, scotus_bound,
        supremecourt_gov,
    )
    from itn.extract import text_citations
    from itn.storage import R2

    r2 = None if args.no_text else R2()
    runners = {
        "federal-register": lambda conn: federal_register.run(
            conn, r2=r2, since_year=args.since, fetch_text=not args.no_text),
        "congress-laws": lambda conn: congress_laws.run(
            conn, r2=r2, since_congress=args.since_congress, fetch_text=not args.no_text,
            max_minutes=args.max_minutes),
        "scotus-bulk": lambda conn: cap_scotus.run(
            conn, r2=r2, fetch_text=not args.no_text, max_minutes=args.max_minutes),
        "scotus-gov": lambda conn: supremecourt_gov.run(
            conn, r2=r2, since_term=args.since_term, fetch_text=not args.no_text,
            max_minutes=args.max_minutes),
        "scotus-bound": lambda conn: scotus_bound.run(
            conn, r2=r2, fetch_text=not args.no_text, max_minutes=args.scotus_minutes),
        "cited-scotus": lambda conn: cited_scotus.run(
            conn, r2=r2, fetch_text=not args.no_text, max_minutes=args.scotus_minutes),
        "text-citations": lambda conn: text_citations.run(conn, r2=r2, max_minutes=args.max_minutes),
        "cited-usc": lambda conn: cited_usc.run(
            conn, r2=r2, fetch_text=not args.no_text, max_minutes=args.scotus_minutes),
        "scotus": lambda conn: courtlistener_scotus.run(
            conn, r2=r2, since=args.since_date, fetch_text=not args.no_text,
            max_minutes=args.scotus_minutes),
    }
    with db.connect() as conn:
        for name in args.sources or list(runners):
            n = runners[name](conn)
            print(f"{name}: {n} documents")


def cmd_publish(args) -> None:
    from itn.publish import export

    with db.connect() as conn:
        version = export.run(conn)
    print("Published version", version)


STATUS_QUERIES = [
    ("Artifacts by type (in scope / cited)",
     "SELECT type, count(*) FILTER (WHERE in_scope), count(*) FILTER (WHERE NOT in_scope)"
     " FROM artifact GROUP BY type ORDER BY type"),
    ("Supreme Court cases in scope by year",
     "SELECT extract(year FROM effective_date)::int, count(*),"
     " count(*) FILTER (WHERE id LIKE 'scotus/docket-%%') FROM artifact"
     " WHERE type = 'scotus_case' AND in_scope GROUP BY 1 ORDER BY 1"),
    ("Supreme Court cases cited by the corpus but not loaded (by U.S. Reports volume range)",
     "SELECT CASE WHEN split_part(split_part(c.to_artifact, '/', 2), '-', 1)::int < 528 THEN 'before 528 (pre-2000)'"
     " WHEN split_part(split_part(c.to_artifact, '/', 2), '-', 1)::int <= 572 THEN '528-572'"
     " ELSE '573 and later' END, count(DISTINCT c.to_artifact)"
     " FROM citation c JOIN component f ON f.id = c.from_component"
     " JOIN artifact src ON src.id = f.artifact_id AND src.closure_depth = 0"
     " LEFT JOIN artifact a ON a.id = c.to_artifact"
     " WHERE c.to_artifact ~ '^scotus/[0-9]+-us-[0-9]+$' AND a.id IS NULL GROUP BY 1 ORDER BY 1"),
    ("Docket-keyed Supreme Court cases by source and year",
     "SELECT metadata->>'source', extract(year FROM effective_date)::int, count(*) FROM artifact"
     " WHERE type = 'scotus_case' AND id LIKE 'scotus/docket-%%' GROUP BY 1, 2 ORDER BY 1, 2"),
    ("Sample of docket-keyed Supreme Court cases",
     "SELECT id, effective_date, left(title, 80), metadata->>'opinions', metadata->>'source'"
     " FROM artifact WHERE type = 'scotus_case' AND id LIKE 'scotus/docket-%%'"
     " ORDER BY random() LIMIT 20"),
    ("Cited 2015+ cases not loaded: opinion pages vs orders pages (page 900 and up)",
     "SELECT CASE WHEN split_part(c.to_artifact, '-us-', 2)::int >= 900 THEN 'orders pages'"
     " ELSE 'opinion pages' END, count(DISTINCT c.to_artifact) FROM citation c"
     " JOIN component f ON f.id = c.from_component"
     " JOIN artifact src ON src.id = f.artifact_id AND src.closure_depth = 0"
     " LEFT JOIN artifact a ON a.id = c.to_artifact"
     " WHERE c.to_artifact ~ '^scotus/(57[3-9]|5[89][0-9]|6[0-9][0-9])-us-[0-9]+$' AND a.id IS NULL"
     " GROUP BY 1"),
    ("Sample of cited 2015+ opinion pages not loaded",
     "SELECT t, raw FROM (SELECT DISTINCT ON (c.to_artifact) c.to_artifact AS t,"
     " left(c.context_snippet, 160) AS raw FROM citation c"
     " JOIN component f ON f.id = c.from_component"
     " JOIN artifact src ON src.id = f.artifact_id AND src.closure_depth = 0"
     " LEFT JOIN artifact a ON a.id = c.to_artifact"
     " WHERE c.to_artifact ~ '^scotus/(57[4-9]|5[89][0-9]|6[0-9][0-9])-us-[0-9]+$' AND a.id IS NULL"
     " AND split_part(c.to_artifact, '-us-', 2)::int < 900) s ORDER BY random() LIMIT 15"),
    ("Cited 2015+ opinion pages not loaded, with the nearest loaded case in that volume",
     "WITH miss AS (SELECT DISTINCT c.to_artifact AS t,"
     " split_part(split_part(c.to_artifact, '/', 2), '-', 1)::int AS vol,"
     " split_part(c.to_artifact, '-us-', 2)::int AS page FROM citation c"
     " JOIN component f ON f.id = c.from_component"
     " JOIN artifact src ON src.id = f.artifact_id AND src.closure_depth = 0"
     " LEFT JOIN artifact a ON a.id = c.to_artifact"
     " WHERE c.to_artifact ~ '^scotus/(57[4-9]|5[89][0-9]|6[0-9][0-9])-us-[0-9]+$' AND a.id IS NULL"
     " AND split_part(c.to_artifact, '-us-', 2)::int < 900),"
     " have AS (SELECT id, split_part(split_part(id, '/', 2), '-', 1)::int AS vol,"
     " split_part(id, '-us-', 2)::int AS page, title FROM artifact"
     " WHERE type = 'scotus_case' AND id ~ '^scotus/[0-9]+-us-[0-9]+$')"
     " SELECT m.t, n.id, n.page - m.page, left(n.title, 50) FROM miss m"
     " LEFT JOIN LATERAL (SELECT h.id, h.page, h.title FROM have h WHERE h.vol = m.vol"
     " ORDER BY abs(h.page - m.page) LIMIT 1) n ON true ORDER BY m.vol, m.page"),
    ("Laws since 2000 by lowest final-passage yes share | ceremonial title | count | with CRS summary over 1,500 chars",
     "WITH l AS (SELECT id, title, length(coalesce(official_summary, '')) AS slen,"
     " (SELECT min((v->>'yeas')::float / nullif((v->>'yeas')::int + (v->>'nays')::int, 0))"
     "  FROM jsonb_array_elements(coalesce(metadata->'votes', '[]'::jsonb)) v) AS share"
     " FROM artifact WHERE type = 'public_law' AND effective_date >= '2000-01-01')"
     " SELECT CASE WHEN share IS NULL THEN '0 no roll call' WHEN share < 0.6 THEN '1 under 60%%'"
     " WHEN share < 2.0/3 THEN '2 60-67%%' WHEN share < 0.8 THEN '3 67-80%%' WHEN share < 0.9 THEN '4 80-90%%'"
     " ELSE '5 90%%+' END,"
     " title ~* '(post office|postal facility|to designate|to name |rename|commemorat|coin|gold medal|courthouse"
     "|land conveyance|to convey|memorial|national day|technical correction|clinic)',"
     " count(*), count(*) FILTER (WHERE slen > 1500) FROM l GROUP BY 1, 2 ORDER BY 1, 2"),
    ("Ingest cursors", "SELECT source, left(cursor::text, 200) FROM ingest_cursor ORDER BY source"),
]


def cmd_status(args) -> None:
    """Free: counts of what is loaded and what is still missing."""
    with db.connect() as conn:
        for title, sql in STATUS_QUERIES:
            print(f"\n{title}")
            for row in conn.execute(sql).fetchall():
                print("  " + " | ".join("" if v is None else str(v) for v in row))
        conn.commit()


def _scope_ids(conn, args) -> list[str]:
    q, params = "SELECT id FROM artifact WHERE in_scope", []
    if args.ids:
        q += " AND id = ANY(%s)"; params.append(args.ids)
    if args.type:
        q += " AND type = %s"; params.append(args.type)
    if args.years:
        lo, _, hi = args.years.partition("-")
        q += " AND extract(year FROM effective_date) BETWEEN %s AND %s"; params += [int(lo), int(hi or lo)]
    if args.unscored:
        q += " AND NOT EXISTS (SELECT 1 FROM score s WHERE s.artifact_id = artifact.id AND s.current)"
    rows = conn.execute(q + " ORDER BY effective_date, id", params).fetchall()
    conn.commit()
    return [r[0] for r in rows]


def _dry_run(args) -> None:
    from itn.analyze import estimate as est
    from itn.analyze.rubric import load_anchors, load_rubric
    from itn.storage import R2

    rubric = load_rubric()
    stages, out = rubric["stages"], []
    run = rubric.get("run", {})
    flags = {"batch": bool(run.get("batch")), "caching": bool(run.get("prompt_caching"))}
    mode = ", ".join(k for k, v in flags.items() if v) or "standard"
    opts = {**flags, "effort": rubric.get("effort")}
    esc = (rubric.get("compare_escalate") or {}).get("to")
    r2 = R2()
    if args.pilot:
        anchors = load_anchors()
        ids, missing = [], []
        conn = db.connect()
        for typ in ("scotus_case", "executive_document", "public_law"):
            for a in anchors.get(typ, []):
                aid = est.resolve_anchor(conn, typ, a)
                (ids.append(aid) if aid else missing.append(f"{typ}: {a.get('id') or a.get('title')}"))
        conn.commit()
        items, sizes = est.gather(conn, r2, ids)
        out += est.report(f"Pilot: {len(ids)} calibration anchors (rubric v{rubric['version']},"
                          f" max tier {args.max_tier}, {mode})", est.plan(items, sizes, stages, args.max_tier, **opts))
        if rubric.get("compare_stages"):
            alt = {**stages, **rubric["compare_stages"]}
            out += est.report("Pilot comparison pass with " + ", ".join(
                f"{k} on {v}" for k, v in rubric["compare_stages"].items())
                + (f", low scores re-assessed on {esc}" if esc else ""),
                est.plan(items, sizes, alt, args.max_tier, escalate_to=esc, **opts))
        if rubric.get("economy"):
            out += est.report("Pilot economy pass (one Haiku pass from free material)",
                              est.plan_economy(items, rubric["economy"], **flags))
        for it in items:
            tag = f"  [routine: {it.presort}]" if it.presort else ""
            out.append(f"    {it.id:28} {it.text_tokens:>8,} tok  {len(it.cited):>3} cites  {it.title[:60]}{tag}")
        if missing:
            out.append("  Anchors not found (left out): " + "; ".join(missing))
        print("\n".join(out), flush=True)
        out = []
    if args.sample:
        grand = compare_total = economy_total = escalated_total = 0.0
        for typ in ("scotus_case", "executive_document", "public_law"):
            conn = db.connect()  # each phase can outlast the database's idle timeout
            ids, total = est.sample_ids(conn, typ, args.sample)
            items, sizes = est.gather(conn, r2, ids)
            e = est.plan(items, sizes, stages, args.max_tier, **opts)
            scale = total / max(len(items), 1)
            for k in e.usd:
                e.usd[k] *= scale
                e.calls[k] *= scale
            e.unloaded_cited = int(e.unloaded_cited * scale)
            e.digests = int(e.digests * scale)
            out += est.report(f"All {total:,} in-scope {typ} ({mode}; scaled from a sample of {len(items)})", e)
            if rubric.get("compare_stages"):
                c = est.plan(items, sizes, {**stages, **rubric["compare_stages"]}, args.max_tier,
                             escalate_to=esc, **opts)
                out.append(f"  with the comparison settings: ${c.total * scale:,.2f}")
                compare_total += c.total * scale
            if rubric.get("economy"):
                ec = est.plan_economy(items, rubric["economy"], **flags)
                out.append(f"  with the economy settings: ${ec.total * scale:,.2f}")
                economy_total += ec.total * scale
                es = est.plan_economy(items, rubric["economy"], escalate_to="sonnet", **flags)
                out.append(f"  economy with disputed items settled on Sonnet: ${es.total * scale:,.2f}")
                escalated_total += es.total * scale
            grand += e.total
            print("\n".join(out), flush=True)
            out = []
        lo, hi = est.RANGE
        out.append(f"== Full corpus, all types ({mode}): ${grand:,.2f} (range ${grand * lo:,.2f} to ${grand * hi:,.2f})")
        if compare_total:
            out.append(f"== With the comparison settings: ${compare_total:,.2f}")
        if economy_total:
            out.append(f"== With the economy settings: ${economy_total:,.2f}"
                       f" (range ${economy_total * lo:,.2f} to ${economy_total * hi:,.2f})")
            out.append(f"== Economy with disputed items settled on Sonnet: ${escalated_total:,.2f}")
    if not args.pilot and not args.sample:
        conn = db.connect()
        ids = _scope_ids(conn, args)
        items, sizes = est.gather(conn, r2, ids)
        out += est.report(f"Scope: {len(ids)} items ({mode})", est.plan(items, sizes, stages, args.max_tier, **opts))
    print("\n".join(out))
    print("\nDry run: no model was called and nothing was spent. Prices are the published rates as of"
          " 2026-10-06; token counts are approximate.")


def cmd_analyze(args) -> None:
    """Paid analysis. Only the `paid` GitHub environment has the API key."""
    scope = {"ids": args.ids, "type": args.type, "years": args.years, "unscored": args.unscored,
             "pilot": args.pilot, "sample": args.sample, "compare": args.compare, "profile": args.profile}
    print(f"Scope: {scope}  budget: ${args.budget:.2f}  max tier: {args.max_tier}  dry run: {args.dry_run}")
    if args.dry_run:
        _dry_run(args)
        return
    if args.budget <= 0:
        sys.exit("A paid run needs --budget greater than 0.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Paid runs only work in the `paid` environment.")
    import anthropic

    from itn.analyze import estimate as est
    from itn.analyze.rubric import load_anchors, load_rubric
    from itn.analyze.stages import Pipeline
    from itn.storage import R2

    with db.connect() as conn:
        if args.pilot:
            ids = [aid for typ, entries in load_anchors().items() if isinstance(entries, list)
                   for a in entries if (aid := est.resolve_anchor(conn, typ, a))]
        else:
            ids = _scope_ids(conn, args)
    if not ids:
        sys.exit("Nothing in scope.")
    print(f"Scoring {len(ids)} items, {args.profile} settings"
          f"{' (trial: scores stay off the site)' if args.compare else ''}", flush=True)
    pipeline = Pipeline(R2(), anthropic.Anthropic(), load_rubric(), args.budget, args.max_tier,
                        compare=args.compare, dispatched_by=os.environ.get("GITHUB_ACTOR"), scope=scope,
                        log=lambda m: print(m, flush=True), profile=args.profile)
    pipeline.score(ids)


def cmd_calibration(args) -> None:
    """Free: each anchor's expected band next to its current score and comparison score."""
    from itn.analyze import estimate as est
    from itn.analyze.rubric import load_anchors
    from itn.publish.export import BANDS

    def band(raw):
        return next(name for floor, name in BANDS if raw * 100 >= floor) if raw is not None else "-"

    hits = {"current": 0, "compare": 0, "economy": 0}
    n = 0
    with db.connect() as conn:
        for typ, entries in load_anchors().items():
            if not isinstance(entries, list):
                continue
            for a in entries:
                aid = est.resolve_anchor(conn, typ, a)
                if not aid:
                    print(f"  {typ}: {a.get('id') or a.get('title')} not found")
                    continue
                cur = conn.execute("SELECT raw FROM score WHERE artifact_id = %s AND current", (aid,)).fetchone()
                def latest(pattern):
                    row = conn.execute(
                        "SELECT s.raw FROM score s JOIN analysis an ON an.id = s.analysis_id"
                        " WHERE s.artifact_id = %s AND an.rubric_version ~ %s"
                        " ORDER BY an.created_at DESC, s.analysis_id DESC LIMIT 1", (aid, pattern)).fetchone()
                    return band(row[0] if row else None)
                b1 = band(cur[0] if cur else None)
                b2, b3 = latest(r"^[0-9]+-compare$"), latest(r"-economy")
                n += 1
                hits["current"] += b1 == a["expected"]
                hits["compare"] += b2 == a["expected"]
                hits["economy"] += b3 == a["expected"]
                print(f"  {aid:28} expected {a['expected']:13} current {b1:13} compare {b2:13} economy {b3:13}")
                if args.detail:
                    row = conn.execute(
                        "SELECT kind, output FROM analysis WHERE subject_id = %s AND rubric_version ~ '-economy'"
                        " AND kind IN ('type_assessment', 'verification') ORDER BY created_at DESC, id DESC LIMIT 1",
                        (aid,)).fetchone()
                    for name, d in ((row[1] or {}).get("dimensions", {}) if row else {}).items():
                        print(f"      {row[0][:6]} {name:10} {d.get('rating')}/{d.get('rating_independent')}"
                              f" evidence {d.get('evidence')}  {(d.get('reason') or '')[:220]}")
        cost = conn.execute("SELECT id, status, cost_usd, scope->>'rubric' FROM run WHERE NOT dry_run"
                            " ORDER BY id DESC LIMIT 5").fetchall()
    print(f"Matches (by raw score, pilot only): current {hits['current']}/{n}, compare {hits['compare']}/{n},"
          f" economy {hits['economy']}/{n}")
    for r in cost:
        print(f"  run {r[0]} {r[1]} ${r[2]} rubric {r[3]}")


def cmd_review(args) -> None:
    """Free: export review packets for a session reviewer, import its answers, or re-apply
    the fixed rules to stored scores."""
    from pathlib import Path

    from itn.analyze import review
    from itn.analyze.rubric import load_rubric

    rubric = load_rubric()
    if args.action == "export":
        from itn.storage import R2
        with db.connect() as conn:
            ids = args.ids or review.select(conn, args.max_raw, args.min_confidence, args.type or None,
                                            args.limit, args.reviewer)
        print(f"Exporting {len(ids)} review packets", flush=True)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        review.export(R2(), rubric, ids, out, log=lambda m: print(m, flush=True))
    elif args.action == "rescore":
        from itn.storage import R2
        review.rescore_rules(R2(), rubric, log=lambda m: print(m, flush=True))
    else:
        review.import_answers(rubric, [Path(p) for p in args.packets], [Path(p) for p in args.answers],
                              args.reviewer, log=lambda m: print(m, flush=True))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="itn")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)

    ing = sub.add_parser("ingest", help="pull public records (free)")
    ing.add_argument("sources", nargs="*", choices=["federal-register", "congress-laws", "scotus-bulk", "scotus-gov", "scotus-bound", "scotus", "cited-scotus", "text-citations", "cited-usc"])
    ing.add_argument("--since", type=int, help="Federal Register: first year to (re)ingest")
    ing.add_argument("--since-congress", type=int, help="Congress.gov: first Congress to (re)ingest")
    ing.add_argument("--since-term", type=int, help="supremecourt.gov: first October Term to (re)ingest")
    ing.add_argument("--since-date", help="Supreme Court: first filing date to (re)ingest, YYYY-MM-DD")
    ing.add_argument("--scotus-minutes", type=float, default=170,
                     help="CourtListener: time budget, long enough to wait out two hourly limits")
    ing.add_argument("--max-minutes", type=float, default=95,
                     help="Congress.gov and Supreme Court: stop cleanly after this long; the next run resumes")
    ing.add_argument("--no-text", action="store_true", help="metadata only, skip R2 uploads")
    ing.set_defaults(fn=cmd_ingest)

    sub.add_parser("publish", help="export static files to R2 (free)").set_defaults(fn=cmd_publish)
    sub.add_parser("status", help="counts of loaded and missing records (free)").set_defaults(fn=cmd_status)

    cal = sub.add_parser("calibration", help="anchor bands vs scores (free)")
    cal.add_argument("--detail", action="store_true", help="also print each economy rating and its evidence")
    cal.set_defaults(fn=cmd_calibration)

    an = sub.add_parser("analyze", help="run paid analysis")
    an.add_argument("--ids", nargs="*", default=[])
    an.add_argument("--type", choices=["scotus_case", "public_law", "executive_document", "action_event"])
    an.add_argument("--years", help="e.g. 2001-2004")
    an.add_argument("--unscored", action="store_true")
    an.add_argument("--budget", type=float, default=0.0, help="hard cap in USD (required for paid runs)")
    an.add_argument("--max-tier", choices=["haiku", "sonnet", "opus"], default="sonnet")
    an.add_argument("--dry-run", action="store_true")
    an.add_argument("--pilot", action="store_true", help="the calibration anchors in calibration/anchors.yaml")
    an.add_argument("--compare", action="store_true",
                    help="trial run: standard profile uses compare_stages; results never become current scores")
    an.add_argument("--profile", choices=["standard", "economy"], default="standard",
                    help="economy: one Haiku pass per item from free material (rubric `economy:`)")
    an.add_argument("--sample", type=int, default=0,
                    help="dry run: also estimate every in-scope item from a random sample of this many per type")
    an.set_defaults(fn=cmd_analyze)

    rv = sub.add_parser("review", help="session review packets and answers (free, no model API)")
    rv.add_argument("action", choices=["export", "import", "rescore"])
    rv.add_argument("--reviewer", default="claude-fable-5-2 (session)", help="recorded as the score's model")
    rv.add_argument("--ids", nargs="*", default=[], help="export: these ids instead of a selection")
    rv.add_argument("--max-raw", type=float, default=0.6, help="export: current raw below this (0.6 = unusual or less normal)")
    rv.add_argument("--min-confidence", type=float, default=0.0, help="export: or confidence below this")
    rv.add_argument("--type", nargs="*", choices=["scotus_case", "public_law", "executive_document", "action_event"])
    rv.add_argument("--limit", type=int, default=100)
    rv.add_argument("--out", default="review-packets.jsonl")
    rv.add_argument("--packets", nargs="*", default=[], help="import: packet files")
    rv.add_argument("--answers", nargs="*", default=[], help="import: answer files")
    rv.set_defaults(fn=cmd_review)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

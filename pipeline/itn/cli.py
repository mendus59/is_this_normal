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
                          f" max tier {args.max_tier})", est.plan(items, sizes, stages, args.max_tier))
        for it in items:
            tag = f"  [routine: {it.presort}]" if it.presort else ""
            out.append(f"    {it.id:28} {it.text_tokens:>8,} tok  {len(it.cited):>3} cites  {it.title[:60]}{tag}")
        if missing:
            out.append("  Anchors not found (left out): " + "; ".join(missing))
        print("\n".join(out), flush=True)
        out = []
    if args.sample:
        grand = 0.0
        for typ in ("scotus_case", "executive_document", "public_law"):
            conn = db.connect()  # each phase can outlast the database's idle timeout
            ids, total = est.sample_ids(conn, typ, args.sample)
            items, sizes = est.gather(conn, r2, ids)
            e = est.plan(items, sizes, stages, args.max_tier)
            scale = total / max(len(items), 1)
            for k in e.usd:
                e.usd[k] *= scale
                e.calls[k] *= scale
            e.unloaded_cited = int(e.unloaded_cited * scale)
            e.digests = int(e.digests * scale)
            out += est.report(f"All {total:,} in-scope {typ} (scaled from a sample of {len(items)};"
                              " digests overcounted because shared precedents are counted once per sample item set)", e)
            grand += e.total
            print("\n".join(out), flush=True)
            out = []
        lo, hi = est.RANGE
        out.append(f"== Full corpus, all types: ${grand:,.2f} (range ${grand * lo:,.2f} to ${grand * hi:,.2f};"
                   f" Batch API about half)")
    if not args.pilot and not args.sample:
        conn = db.connect()
        ids = _scope_ids(conn, args)
        items, sizes = est.gather(conn, r2, ids)
        out += est.report(f"Scope: {len(ids)} items", est.plan(items, sizes, stages, args.max_tier))
    print("\n".join(out))
    print("\nDry run: no model was called and nothing was spent. Prices are the published rates as of"
          " 2026-10-06; token counts are approximate.")


def cmd_analyze(args) -> None:
    """Paid analysis. Only the `paid` GitHub environment has the API key."""
    scope = {"ids": args.ids, "type": args.type, "years": args.years, "unscored": args.unscored,
             "pilot": args.pilot, "sample": args.sample}
    print(f"Scope: {scope}  budget: ${args.budget:.2f}  max tier: {args.max_tier}  dry run: {args.dry_run}")
    if args.dry_run:
        _dry_run(args)
        return
    if args.budget <= 0:
        sys.exit("A paid run needs --budget greater than 0.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Paid runs only work in the `paid` environment.")
    sys.exit("The paid stages are not built yet. Nothing was run and nothing was spent.")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="itn")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)

    ing = sub.add_parser("ingest", help="pull public records (free)")
    ing.add_argument("sources", nargs="*", choices=["federal-register", "congress-laws", "scotus-bulk", "scotus-gov", "scotus-bound", "scotus", "cited-scotus", "cited-usc"])
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

    an = sub.add_parser("analyze", help="run paid analysis")
    an.add_argument("--ids", nargs="*", default=[])
    an.add_argument("--type", choices=["scotus_case", "public_law", "executive_document", "action_event"])
    an.add_argument("--years", help="e.g. 2001-2004")
    an.add_argument("--unscored", action="store_true")
    an.add_argument("--budget", type=float, default=0.0, help="hard cap in USD (required for paid runs)")
    an.add_argument("--max-tier", choices=["haiku", "sonnet", "opus"], default="sonnet")
    an.add_argument("--dry-run", action="store_true")
    an.add_argument("--pilot", action="store_true", help="the calibration anchors in calibration/anchors.yaml")
    an.add_argument("--sample", type=int, default=0,
                    help="dry run: also estimate every in-scope item from a random sample of this many per type")
    an.set_defaults(fn=cmd_analyze)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

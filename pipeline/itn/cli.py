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


def cmd_analyze(args) -> None:
    """Paid analysis. Only the `paid` GitHub environment has the API key."""
    scope = {"ids": args.ids, "type": args.type, "years": args.years, "unscored": args.unscored}
    print(f"Scope: {scope}  budget: ${args.budget:.2f}  max tier: {args.max_tier}  dry run: {args.dry_run}")
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Paid runs only work in the `paid` environment.")
    sys.exit("Analysis stages arrive in phase 4. Nothing was run and nothing was spent.")


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
    an.add_argument("--budget", type=float, required=True, help="hard cap in USD")
    an.add_argument("--max-tier", choices=["haiku", "sonnet", "opus"], default="sonnet")
    an.add_argument("--dry-run", action="store_true")
    an.set_defaults(fn=cmd_analyze)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

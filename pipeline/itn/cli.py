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
    from itn.ingest import congress_laws, courtlistener_scotus, federal_register
    from itn.storage import R2

    r2 = None if args.no_text else R2()
    runners = {
        "federal-register": lambda conn: federal_register.run(
            conn, r2=r2, since_year=args.since, fetch_text=not args.no_text),
        "congress-laws": lambda conn: congress_laws.run(
            conn, r2=r2, since_congress=args.since_congress, fetch_text=not args.no_text,
            max_minutes=args.max_minutes),
        "scotus": lambda conn: courtlistener_scotus.run(
            conn, r2=r2, since=args.since_date, fetch_text=not args.no_text,
            max_minutes=args.max_minutes),
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
    ing.add_argument("sources", nargs="*", choices=["federal-register", "congress-laws", "scotus"])
    ing.add_argument("--since", type=int, help="Federal Register: first year to (re)ingest")
    ing.add_argument("--since-congress", type=int, help="Congress.gov: first Congress to (re)ingest")
    ing.add_argument("--since-date", help="Supreme Court: first filing date to (re)ingest, YYYY-MM-DD")
    ing.add_argument("--max-minutes", type=float, default=95,
                     help="Congress.gov and Supreme Court: stop cleanly after this long; the next run resumes")
    ing.add_argument("--no-text", action="store_true", help="metadata only, skip R2 uploads")
    ing.set_defaults(fn=cmd_ingest)

    sub.add_parser("publish", help="export static files to R2 (free)").set_defaults(fn=cmd_publish)

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

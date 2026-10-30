# IsThisNormal

Is a Supreme Court ruling, executive action, or contentious law "normal"? This
project measures each one since 2000 against the precedent, statute text,
comparable actions, and public data that existed when it happened.

- **`web/`**: the static site (Astro), deployed to GitHub Pages. No cookies or analytics.
- **`pipeline/`**: the `itn` command: free ingestion of public records, export to
  static files, and paid AI analysis that only runs when you start it.
- **`schema/`**: Postgres migrations.
- **`rubrics/`**, **`calibration/`**: versioned scoring rubrics and anchor artifacts (phase 4).

Data lives in Cloudflare R2 (documents and published files) and Neon Postgres
(citation graph and analyses). See [SETUP.md](SETUP.md) to connect them.

## Workflows

| Workflow | Trigger | Spends money? |
|---|---|---|
| Ingest | daily, or by hand | no |
| Publish data | daily, after Analyze, or by hand | no |
| Analyze | by hand only, after you approve the `paid` environment | yes, up to the budget you set |
| Deploy site | changes under `web/` | no |
| CI | pull requests | no |

## Status

Phase 1: storage, schema, workflows, Federal Register ingestion, and a browse
page. Next: Congress.gov and GovInfo laws with vote-based contentiousness, then
Supreme Court opinions and citation extraction.

# IsThisNormal

- Pipeline: Python package `itn` in `pipeline/`; run tests with `cd pipeline && pytest`.
- Site: Astro in `web/`; `npm run build` must pass.
- Artifact IDs follow `pipeline/itn/ids.py`; never invent another format.
- Anything that calls a model API belongs behind the `analyze` command and the `paid`
  GitHub environment. Scheduled workflows must never receive `ANTHROPIC_API_KEY`.
- Analysis inputs must be filtered to material dated before the artifact's `as_of` date in
  the data layer, not only by prompt instructions.

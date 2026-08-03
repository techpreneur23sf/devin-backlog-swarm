# Superset: patch/minor dependency bump

Mechanical work with one trap: the pin appears in more than one file.

## Procedure

1. Move the pin in every `requirements/*.txt` the issue names, and in the
   matching `requirements/*.in` if the package is named there — Superset
   compiles the `.txt` from the `.in` with `pip-compile`.
2. Keep the diff to the pin. No re-compilation of unrelated lines, no other
   dependency updates, no formatting.
3. Run exactly the issue's verification commands. Never the full test suite,
   never a frontend build, never `docker compose up`.

## Report

- If the bump needs code changes, the issue is mis-classified: `outcome:
  partial` with the reason, not a larger PR.
- If another pin caps the target version, bumping that pin is part of the task —
  name it in the PR body.
- `confidence: high` requires that you ran the verification commands yourself.

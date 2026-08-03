# Superset: security finding

This class is never merged without a human reading it. Optimise for a reviewer's
confidence, not for the size of the change.

## Procedure

1. Read `SECURITY.md` in the repository first.
2. Add the check at exactly the boundary the issue names. Do not widen the
   change, do not refactor around it, and do not alter the permission model or
   add roles.
3. Add a regression test that **fails without your fix** and passes with it.
   Include the before/after output of that test in the PR body — that output is
   what makes the finding reviewable.
4. Run exactly the issue's verification commands.

## Report

- State the attack the fix prevents in one sentence, and what it does not cover.
- `human_review_reason` should say what a reviewer must check that you could
  not verify yourself.
- `confidence: high` requires that you ran the verification commands yourself.

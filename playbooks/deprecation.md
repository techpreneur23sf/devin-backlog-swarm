# Superset: deprecation migration

The correct replacement differs by call site. A blind find-and-replace is the
failure mode for this class, not the solution.

## Procedure

1. Read every call site in the declared touch scope before changing any of it.
2. Choose the replacement per call site. Where semantics could differ — timezone
   awareness, naming, default arguments, ordering — decide deliberately and
   state the reasoning in the PR body.
3. Where the old and new behaviour differ at a boundary that is not covered by a
   test, add the test.
4. Run exactly the issue's verification commands.

## Report

- Call out any call site you deliberately left alone, and why.
- `outcome: partial` if the migration cannot be completed inside the touch
  scope. Do not widen the scope silently.
- `confidence: high` requires that you ran the verification commands yourself.

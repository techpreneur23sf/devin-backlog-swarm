# Superset: correctness and dead-code cleanup

## Procedure

1. Remove dead branches entirely rather than hiding them behind a constant, and
   delete the flag or setting that selected them if the issue says to.
2. Update the tests that covered the removed behaviour instead of deleting them
   wholesale; a deleted test is a silent loss of coverage.
3. New or modified Python in this repository carries type hints and must satisfy
   the repo's own lint and mypy configuration. New source files need the Apache
   licence header.
4. Run exactly the issue's verification commands.

## Report

- List anything you found that is out of scope but worth filing, rather than
  fixing it here.
- `confidence: high` requires that you ran the verification commands yourself.

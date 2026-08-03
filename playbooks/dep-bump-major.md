# Superset: major-version dependency bump

A major bump is expected to break call sites. A version bump alone is a failed
task here.

## Procedure

1. Read the library's changelog between the pinned version and the target, and
   list the breaking changes that touch this repository.
2. Move the pin in every `requirements/*.txt` the issue names. Superset compiles
   those from the matching `requirements/*.in` with `pip-compile`; if the
   package is named in the `.in` file, update it there too. Do not re-compile
   the whole file — the diff must be the pin and its consequences.
3. Check for pins that cap the target version transitively. If another package
   holds the old version in place, bumping it is part of this task; say so in
   the PR body.
4. Update the call sites the changelog implicated, inside the declared touch
   scope only, and the tests covering them.
5. Run exactly the issue's verification commands. Do not run Superset's full
   test suite and do not build the frontend.

## Report

- `outcome: fixed` only if the verification commands ran and passed.
- `outcome: partial` if the bump needs work outside the touch scope — name the
  files it would need.
- `outcome: wont_fix` with reasoning if the bump is wrong, e.g. the fixed
  version does not exist yet or the advisory does not apply to how this
  repository uses the package. A refusal with a reason is a valid result.
- `confidence: high` requires that you ran the verification commands yourself.

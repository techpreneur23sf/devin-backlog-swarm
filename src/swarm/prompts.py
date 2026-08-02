"""Prompt construction, per issue class.

Sessions fail on Superset for one reason above all others: they try to build
the whole application. Every prompt therefore pins the verification surface to
the exact commands the issue declares and forbids full-suite runs.
"""

from __future__ import annotations

from .issues import IssueSpec

BASE_RULES = """
Ground rules for this task (they matter more than speed):

- Work only inside the declared touch scope: {scope}. Do not reformat or
  refactor code outside it, and do not update unrelated dependencies.
- Verification is exactly these commands, nothing broader:
{verify_block}
  Do NOT run Superset's full test suite, do NOT build the frontend, and do NOT
  attempt `docker compose up`. If setting up the environment for the declared
  commands takes more than a few minutes, stop and report `blocked` with the
  reason rather than burning the budget.
- Open exactly one pull request against `{base}`, titled conventionally
  (`fix(...)`, `chore(deps): ...`, `refactor(...)`), whose description explains
  what changed, why, and the verification output.
- The PR body must contain the line `Closes #{issue}`.
- If the change turns out to be wrong, unnecessary, or larger than the issue
  describes, report `wont_fix` or `partial` with a clear reason. A truthful
  "this needs a human" is worth more than a speculative patch.
- Finish by calling provide_structured_output with is_final=true and the schema
  you were given. `confidence` and `blockers` gate auto-merge, so be honest:
  claim `high` only if you ran the verification commands and they passed.
"""

CLASS_GUIDANCE = {
    "dep-bump-patch": """
This is a patch/minor dependency bump for a known vulnerability. Change the pin
in the requirements file(s), keep the change minimal, and confirm nothing else
in the lock/constraint files drifts. If the bump requires code changes, that is
a signal the issue is mis-classified: report `partial` and explain.
""",
    "dep-bump-major": """
This is a major-version bump that is expected to break call sites. Read the
library's changelog for the breaking changes, then update every call site in
the touch scope and the tests that cover them. A version bump alone is a failed
task here.
""",
    "deprecation": """
This is a deprecation migration. The correct replacement differs by call site,
so read each one before changing it; a blind find-and-replace is a failure mode
for this task, not a solution. Where semantics could change (timezones, naming,
default arguments), state the reasoning in the PR body.
""",
    "code-quality": """
This is a correctness / dead-code cleanup. Remove the dead branches entirely
rather than leaving them behind a constant, and make sure the tests that
covered the removed behaviour are updated rather than deleted wholesale.
""",
    "security": """
This is a security finding. Add the check at the boundary the issue names, and
add a regression test that FAILS without your fix and passes with it — include
the before/after output of that test in the PR body. Do not widen the change
beyond the boundary. This class is never auto-merged; a human will read it.
""",
    "authz": """
This is an authorization gap. Add the check at the boundary the issue names and
a regression test that fails before the fix. Do not change the permission model
or add new roles. This class is never auto-merged; a human will read it.
""",
}


def build_prompt(spec: IssueSpec, repo: str, base_branch: str = "master") -> str:
    verify = spec.verify or ["python -m pytest -q <the tests covering your change>"]
    verify_block = "\n".join(f"    {c}" for c in verify)
    guidance = CLASS_GUIDANCE.get(spec.issue_class, "").strip()
    return f"""You are remediating a tracked backlog issue in the repository `{repo}`.

## Issue #{spec.number}: {spec.title}

{spec.body}

## How to approach it

{guidance}

{BASE_RULES.format(scope=", ".join(spec.touch_scope), verify_block=verify_block, base=base_branch, issue=spec.number)}
"""


def review_prompt(pr_url: str, spec: IssueSpec) -> str:
    """Fallback reviewer prompt, used when the Devin Review API is unavailable."""
    return f"""Review the pull request {pr_url}, which claims to resolve issue #{spec.number}
("{spec.title}") in class `{spec.issue_class}`.

Do not push commits. Read the diff and judge it against the issue's acceptance
criteria and verification commands. Check specifically for: changes outside the
declared touch scope ({", ".join(spec.touch_scope)}), missing test coverage for
the behaviour that changed, and semantics that silently differ from the
original code.

Post your review as a PR comment, then return structured output with
`verdict` one of clean / comments / blocking, and `findings` as a list.
"""


REVIEW_SCHEMA = {
    "type": "object",
    "required": ["verdict", "summary"],
    "properties": {
        "verdict": {"type": "string", "enum": ["clean", "comments", "blocking"]},
        "summary": {"type": "string", "maxLength": 600},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
}

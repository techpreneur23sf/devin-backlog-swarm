"""CI for swarm PRs: run the issue's own verification commands.

The fork's inherited Superset workflows are disabled (they are enormous and
were never going to run on this budget), so a swarm PR would otherwise have no
CI signal at all — and the merge policy refuses to auto-merge without one.

Rather than invent a generic pipeline, this turns the contract already written
into the issue into the pipeline: the `verify` list in the issue's `swarm-meta`
block *is* the definition of done, so CI runs exactly those commands, plus a
scope check that the diff stayed inside the declared `touch_scope`. An issue
that declares nothing verifiable therefore cannot produce a green PR, which is
the correct incentive on the issue author.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from .gh import GitHubClient
from .issues import IssueSpec

CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)


@dataclass
class Step:
    name: str
    ok: bool
    output: str


def linked_issue(gh: GitHubClient, pr_number: int) -> int | None:
    pr = gh.get_pr(pr_number)
    for text in (pr.get("body") or "", pr.get("title") or ""):
        m = CLOSES_RE.search(text)
        if m:
            return int(m.group(1))
    return None


def scope_violations(files: Sequence[str], touch_scope: Sequence[str]) -> list[str]:
    """Files the PR touched that no declared scope covers.

    A glob like `superset/utils/**` is matched both as a prefix and via
    fnmatch, because `fnmatch` alone treats `**` as a single path segment.
    """
    if not touch_scope or "**" in touch_scope:
        return []
    out = []
    for path in files:
        if not any(_covered(path, glob) for glob in touch_scope):
            out.append(path)
    return out


def _covered(path: str, glob: str) -> bool:
    if fnmatch.fnmatch(path, glob):
        return True
    if glob.endswith("/**") and path.startswith(glob[:-2]):
        return True
    if glob.endswith("**") and path.startswith(glob[:-2]):
        return True
    return False


def run(
    gh: GitHubClient,
    pr_number: int,
    repo_dir: str = ".",
    timeout: int = 900,
) -> tuple[bool, list[Step], IssueSpec | None]:
    issue_number = linked_issue(gh, pr_number)
    if issue_number is None:
        return False, [Step("resolve linked issue", False, "PR body has no `Closes #N` reference")], None

    spec = IssueSpec.from_issue(gh.get_issue(issue_number))
    steps: list[Step] = []

    changed = [f["filename"] for f in gh.list_pr_files(pr_number)]
    stray = scope_violations(changed, spec.touch_scope)
    steps.append(
        Step(
            f"diff stays within declared scope ({', '.join(spec.touch_scope)})",
            not stray,
            "out of scope: " + ", ".join(stray) if stray else f"{len(changed)} files, all in scope",
        )
    )

    if not spec.verify:
        steps.append(Step("issue declares verification commands", False, "no `verify` in swarm-meta"))
        return False, steps, spec

    for command in spec.verify:
        try:
            proc = subprocess.run(
                command, shell=True, cwd=repo_dir, capture_output=True, text=True, timeout=timeout
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            steps.append(Step(command, proc.returncode == 0, output[-4000:]))
        except subprocess.TimeoutExpired:
            steps.append(Step(command, False, f"timed out after {timeout}s"))

    return all(s.ok for s in steps), steps, spec


def summary(pr_number: int, ok: bool, steps: Sequence[Step], spec: IssueSpec | None) -> str:
    head = f"## Swarm verify · PR #{pr_number}"
    if spec:
        head += f" · issue #{spec.number} (`{spec.issue_class}`)"
    lines = [head, "", f"**{'passed' if ok else 'failed'}** — {sum(s.ok for s in steps)}/{len(steps)} checks", ""]
    for s in steps:
        lines.append(f"<details><summary>{'PASS' if s.ok else 'FAIL'} · <code>{s.name}</code></summary>\n")
        lines.append("```")
        lines.append(s.output.strip() or "(no output)")
        lines.append("```\n</details>")
    return "\n".join(lines) + "\n"

"""Dispatch: issue -> Devin session.

Dispatch is deliberately dumb and fast. It never waits for a session (a CI
runner must not block on an agent), and it is dedupe-keyed on the issue number
so a retried workflow, an overlapping cron and a manual run cannot produce
three sessions for one issue.
"""

from __future__ import annotations

from .devin import REMEDIATION_SCHEMA, DevinClient
from .gh import GitHubClient
from .issues import IssueSpec
from .models import DISPATCHED, Ledger, Task, now
from .policy import Policy
from .prompts import build_prompt

BOT_ACTORS = {"devin-ai-integration[bot]", "github-actions[bot]"}


def session_tags(repo: str, issue_number: int, issue_class: str, run_id: str | None) -> list[str]:
    owner, name = repo.split("/", 1)
    tags = ["swarm", f"repo:{owner}-{name}", f"issue:{issue_number}", f"class:{issue_class}"]
    if run_id:
        tags.append(f"run:{run_id}")
    return tags


def _playbook_id(devin: DevinClient, policy: Policy, issue_class: str) -> str | None:
    """Bind the class's playbook, by title unless policy names an id outright.

    Missing is not fatal: a playbook is the org's durable procedure for a class
    of work, and the issue still carries the task. Dispatching without one is
    worse than dispatching nothing.
    """
    ref = policy.playbook_for(issue_class)
    if not ref:
        return None
    if ref.startswith("playbook-"):
        return ref
    return devin.playbook_id_for_title(ref)


def already_dispatched(ledger: Ledger, issue_number: int) -> Task | None:
    task = ledger.get(issue_number)
    if task and task.session_id and task.state not in ("queued", "failed"):
        return task
    return None


def dispatch_issue(
    gh: GitHubClient,
    devin: DevinClient,
    ledger: Ledger,
    policy: Policy,
    issue_number: int,
    run_id: str | None = None,
    base_branch: str = "master",
    dry_run: bool = False,
) -> tuple[Task, str]:
    """Create (at most) one session for `issue_number`. Returns (task, note)."""
    existing = already_dispatched(ledger, issue_number)
    if existing:
        return existing, f"already dispatched (session {existing.session_id}), no-op"

    issue = gh.get_issue(issue_number)
    spec = IssueSpec.from_issue(issue)
    task = ledger.get(issue_number) or Task(issue_number=issue_number)
    task.issue_title = spec.title
    task.issue_class = spec.issue_class
    task.touch_scope = spec.touch_scope
    ledger.upsert(task)

    if policy.kill_switch:
        return task, "kill switch engaged; not dispatching"
    if task.attempts >= policy.budget.max_attempts_per_issue:
        task.transition("abandoned", "max attempts reached")
        return task, "max attempts reached; abandoned"

    prompt = build_prompt(spec, gh.repo, base_branch)
    tags = session_tags(gh.repo, issue_number, spec.issue_class, run_id)
    acu_limit = policy.acu_limit(spec.issue_class)
    playbook_id = _playbook_id(devin, policy, spec.issue_class)

    if dry_run:
        return task, f"[dry-run] would create session: tags={tags} max_acu={acu_limit} playbook={playbook_id}"

    session = devin.create_session(
        prompt=prompt,
        title=f"[swarm] #{issue_number} {spec.title}"[:200],
        tags=tags,
        repos=[f"https://github.com/{gh.repo}"],
        playbook_id=playbook_id,
        max_acu_limit=acu_limit,
        structured_output_schema=REMEDIATION_SCHEMA,
    )

    task.session_id = session.get("session_id")
    task.session_url = session.get("url") or f"https://app.devin.ai/sessions/{task.session_id}"
    task.session_status = session.get("status")
    task.attempts += 1
    task.dispatched_at = now()
    task.transition(DISPATCHED, f"session {task.session_id} created")

    gh.comment(
        issue_number,
        f"🤖 **Dispatched to Devin** (attempt {task.attempts}/{policy.budget.max_attempts_per_issue})\n\n"
        f"- Session: {task.session_url}\n"
        f"- Class: `{spec.issue_class}` · ACU cap: `{acu_limit}` · Touch scope: "
        f"{', '.join('`' + s + '`' for s in spec.touch_scope)}\n"
        f"- Verification: {', '.join('`' + v + '`' for v in spec.verify) or '_declared in the issue body_'}\n\n"
        f"<sub>The reconciler will report progress here. State: `{task.state}`.</sub>",
    )
    _sync_labels(gh, issue_number, task.state)
    return task, f"dispatched session {task.session_id}"


def _sync_labels(gh: GitHubClient, issue_number: int, state: str) -> None:
    """Issue labels carry coarse, human-visible state. Best effort."""
    try:
        issue = gh.get_issue(issue_number)
        current = [lbl["name"] for lbl in issue.get("labels", [])]
        keep = [lbl for lbl in current if not lbl.startswith("swarm:")]
        gh.set_labels(issue_number, keep + [f"swarm:{state}"])
    except Exception:  # labels are a convenience, never a correctness input
        pass

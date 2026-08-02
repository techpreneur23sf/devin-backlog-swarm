"""The reconciler.

The Devin API does not call back when a session finishes, so the swarm is built
the way a Kubernetes controller is: events record desired state, and a loop
repeatedly makes observed reality match it. Every transition below is derived
from something the APIs said this tick — session status, PR state, CI checks,
review verdict — never from what a previous run remembered doing.

That property is what makes the loop survive crashes, missed webhooks and
duplicate deliveries, and it is why `reconcile()` is safe to run concurrently
with itself.
"""

from __future__ import annotations

import re
from typing import Any

from .devin import DevinClient
from .dispatch import _sync_labels
from .gh import GitHubClient
from .models import (
    ABANDONED,
    DISPATCHED,
    FAILED,
    FAILURE_ACU_CAP,
    FAILURE_AMBIGUOUS,
    FAILURE_ENV_SETUP,
    FAILURE_NO_PR,
    FAILURE_SESSION_ERROR,
    FAILURE_TESTS,
    MERGED,
    NEEDS_HUMAN,
    PR_OPEN,
    QUEUED,
    REVIEW_CLEAN,
    REVIEW_PENDING,
    RUNNING,
    WONT_FIX,
    Ledger,
    Task,
    now,
)
from .policy import Policy, evaluate_merge

PR_URL_RE = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")


def _pr_number(pr_url: str) -> int | None:
    m = PR_URL_RE.match(pr_url or "")
    return int(m.group(2)) if m else None


def _classify_failure(session: dict[str, Any], so: dict[str, Any] | None) -> str:
    detail = (session.get("status_detail") or "").lower()
    blockers = " ".join((so or {}).get("blockers") or []).lower()
    text = f"{detail} {blockers} {((so or {}).get('summary') or '').lower()}"
    if "usage_limit" in detail or "acu" in text or "out_of_credits" in detail:
        return FAILURE_ACU_CAP
    if any(k in text for k in ("install", "dependency", "environment", "setup", "docker", "venv")):
        return FAILURE_ENV_SETUP
    if any(k in text for k in ("test failed", "tests failed", "pytest", "assertion")):
        return FAILURE_TESTS
    if any(k in text for k in ("unclear", "ambiguous", "not reproducible", "cannot find")):
        return FAILURE_AMBIGUOUS
    if detail == "error":
        return FAILURE_SESSION_ERROR
    return FAILURE_NO_PR


#: Devin Review publishes a commit status of its own. It is a review signal, not
#: a test run, and it is evaluated separately — counting it as CI would let a PR
#: merge on the strength of the reviewer agreeing with the author.
NON_CI_CONTEXTS = {"devin review", "devin-review"}


def ci_status_for(gh: GitHubClient, pr: dict[str, Any]) -> str:
    """green / red / pending / none, from the checks + statuses APIs."""
    sha = (pr.get("head") or {}).get("sha")
    if not sha:
        return "unknown"
    states: list[str] = []
    combined = gh.combined_status(sha) or {}
    for status in combined.get("statuses") or []:
        if (status.get("context") or "").strip().lower() in NON_CI_CONTEXTS:
            continue
        states.append(status.get("state", "pending"))
    runs = (gh.check_runs(sha) or {}).get("check_runs") or []
    for run in runs:
        if (run.get("name") or "").strip().lower() in NON_CI_CONTEXTS:
            continue
        if run.get("status") != "completed":
            states.append("pending")
        elif run.get("conclusion") in ("success", "neutral", "skipped"):
            states.append("success")
        else:
            states.append("failure")
    if not states:
        return "none"
    if any(s in ("failure", "error") for s in states):
        return "red"
    if any(s == "pending" for s in states):
        return "pending"
    return "green"


def review_status_for(devin: DevinClient, pr_url: str) -> tuple[str, dict[str, Any] | None]:
    """clean / comments / blocking / pending, from the Devin Review API."""
    try:
        data = devin.get_pr_review(pr_url) or {}
    except Exception:
        return "pending", None
    items = data.get("items") if isinstance(data, dict) else None
    review = (items or [None])[0] if items else (data if isinstance(data, dict) and data.get("status") else None)
    if not review:
        return "pending", None
    status = (review.get("status") or "").lower()
    verdict = (review.get("verdict") or review.get("result") or "").lower()
    if status in ("running", "queued", "pending", "in_progress"):
        return "pending", review
    if verdict in ("clean", "approved", "no_issues", "lgtm"):
        return "clean", review
    if verdict in ("blocking", "changes_requested", "issues_found"):
        return "blocking", review
    comment_count = review.get("comment_count")
    if isinstance(comment_count, int):
        return ("clean" if comment_count == 0 else "comments"), review
    return "pending", review


def reconcile(
    gh: GitHubClient,
    devin: DevinClient,
    ledger: Ledger,
    policy: Policy,
    dry_run: bool = False,
) -> list[str]:
    """One tick. Returns a list of human-readable transitions applied."""
    log: list[str] = []
    if policy.kill_switch:
        return ["kill switch engaged: no sessions polled, no merges performed"]

    for task in list(ledger.tasks.values()):
        if task.is_terminal or task.state == QUEUED:
            continue
        try:
            log.extend(_reconcile_task(gh, devin, ledger, policy, task, dry_run))
        except Exception as exc:  # one bad task must not stall the loop
            task.last_error = f"{type(exc).__name__}: {exc}"[:500]
            log.append(f"#{task.issue_number}: reconcile error: {task.last_error}")
    return log


def _reconcile_task(
    gh: GitHubClient,
    devin: DevinClient,
    ledger: Ledger,
    policy: Policy,
    task: Task,
    dry_run: bool,
) -> list[str]:
    log: list[str] = []
    prev_state = task.state

    session: dict[str, Any] = {}
    if task.session_id:
        session = devin.get_session(task.session_id) or {}
        task.session_status = session.get("status")
        task.session_status_detail = session.get("status_detail")
        task.acus_consumed = float(session.get("acus_consumed") or task.acus_consumed or 0)
        if session.get("structured_output"):
            task.structured_output = session["structured_output"]

    # 1. Session-derived state -------------------------------------------------
    if task.session_status_detail == "waiting_for_user":
        task.ever_waited_for_user = True
        task.waiting_ticks += 1
        if task.waiting_ticks >= policy.budget.waiting_for_user_ticks and not task.pr_url:
            if not dry_run:
                devin.terminate(task.session_id or "")
            if task.transition(NEEDS_HUMAN, "session idle in waiting_for_user; terminated to stop ACU drain"):
                log.append(f"#{task.issue_number}: needs_human (waiting_for_user x{task.waiting_ticks})")
    elif task.state == DISPATCHED and task.session_status in ("running", "resuming", "claimed"):
        if task.transition(RUNNING, "session is working"):
            log.append(f"#{task.issue_number}: running")

    # 2. PR discovery ----------------------------------------------------------
    if not task.pr_url:
        pr_url = _find_pr(session, task)
        if pr_url:
            task.pr_url = pr_url
            task.pr_number = _pr_number(pr_url)
            task.pr_opened_at = now()
            if task.transition(PR_OPEN, "pull request opened"):
                log.append(f"#{task.issue_number}: pr_open {pr_url}")
                if not dry_run:
                    _comment_pr_opened(gh, devin, task, policy)

    # 3. Session finished without a PR ----------------------------------------
    if task.session_status in ("exit", "error", "suspended") and not task.pr_url:
        so = task.structured_output or {}
        outcome = so.get("outcome")
        if outcome == "wont_fix":
            if task.transition(WONT_FIX, so.get("summary") or "session reported wont_fix"):
                log.append(f"#{task.issue_number}: wont_fix")
                if not dry_run:
                    gh.comment(task.issue_number, _outcome_comment(task, "won't fix"))
        elif outcome in ("blocked", "not_reproducible") or task.session_status == "error":
            task.failure_category = _classify_failure(session, so)
            target = NEEDS_HUMAN if outcome else FAILED
            if task.transition(target, f"session ended: {task.failure_category}"):
                log.append(f"#{task.issue_number}: {target} ({task.failure_category})")
                if not dry_run:
                    gh.comment(task.issue_number, _outcome_comment(task, target.replace("_", " ")))
        elif task.session_status == "exit":
            task.failure_category = FAILURE_NO_PR
            if task.transition(FAILED, "session exited without a pull request"):
                log.append(f"#{task.issue_number}: failed (no PR)")

    # 4. PR lifecycle ----------------------------------------------------------
    if task.pr_url and task.pr_number and task.state in (PR_OPEN, REVIEW_PENDING, REVIEW_CLEAN):
        pr = gh.get_pr(task.pr_number)
        task.pr_state = "merged" if pr.get("merged") else pr.get("state")
        if pr.get("merged"):
            if task.transition(MERGED, "pull request merged"):
                log.append(f"#{task.issue_number}: merged {task.pr_url}")
        elif pr.get("state") == "closed":
            if task.transition(ABANDONED, "pull request closed without merging"):
                log.append(f"#{task.issue_number}: abandoned (PR closed)")
        else:
            task.ci_status = ci_status_for(gh, pr)
            if task.state == PR_OPEN:
                if not dry_run:
                    devin.request_pr_review(task.pr_url)
                if task.transition(REVIEW_PENDING, "Devin Review requested"):
                    log.append(f"#{task.issue_number}: review_pending")
            if task.state in (REVIEW_PENDING, REVIEW_CLEAN):
                task.review_status, _ = review_status_for(devin, task.pr_url)
                if task.review_status == "clean" and task.state == REVIEW_PENDING:
                    if task.transition(REVIEW_CLEAN, "review clean"):
                        log.append(f"#{task.issue_number}: review_clean")
                elif task.review_status == "blocking":
                    if task.transition(NEEDS_HUMAN, "Devin Review raised blocking findings"):
                        log.append(f"#{task.issue_number}: needs_human (blocking review)")

            # 5. Merge decision ------------------------------------------------
            if task.state == REVIEW_CLEAN and task.ci_status == "green":
                files = [f["filename"] for f in gh.list_pr_files(task.pr_number)]
                decision = evaluate_merge(
                    policy, task.issue_class, task.ci_status, task.review_status, task.structured_output, files
                )
                if decision.allowed:
                    if not dry_run:
                        gh.merge_pr(task.pr_number, title=f"{_pr_title(gh, task)} (#{task.pr_number})")
                        merged = gh.get_pr(task.pr_number)
                        if not merged.get("merged"):
                            task.last_error = "merge call did not take effect"
                    if dry_run or gh.get_pr(task.pr_number).get("merged"):
                        task.pr_state = "merged"
                        if task.transition(MERGED, decision.reason):
                            log.append(f"#{task.issue_number}: merged automatically ({decision.reason})")
                            if not dry_run:
                                gh.comment(task.issue_number, _merge_comment(task, decision.reason))
                else:
                    if task.transition(NEEDS_HUMAN, decision.reason):
                        log.append(f"#{task.issue_number}: needs_human ({decision.reason})")
                        if not dry_run:
                            gh.comment(
                                task.issue_number,
                                f"🧑‍⚖️ Auto-merge withheld: {decision.reason}. "
                                f"PR {task.pr_url} is ready for a human.",
                            )

    if task.state != prev_state and not dry_run:
        _sync_labels(gh, task.issue_number, task.state)
    return log


def _find_pr(session: dict[str, Any], task: Task) -> str | None:
    for pr in session.get("pull_requests") or []:
        url = pr.get("url") or pr.get("html_url")
        if url:
            return url
    so = task.structured_output or {}
    if so.get("pr_url"):
        return so["pr_url"]
    return None


def _pr_title(gh: GitHubClient, task: Task) -> str:
    try:
        return gh.get_pr(task.pr_number or 0).get("title", task.issue_title)
    except Exception:
        return task.issue_title


def _outcome_comment(task: Task, label: str) -> str:
    so = task.structured_output or {}
    blockers = so.get("blockers") or []
    return (
        f"⚠️ **Session ended: {label}**\n\n"
        f"- Session: {task.session_url}\n"
        f"- Outcome: `{so.get('outcome', 'unknown')}` · confidence `{so.get('confidence', 'n/a')}`\n"
        f"- ACUs spent: `{task.acus_consumed:.2f}`\n"
        f"- Failure category: `{task.failure_category or 'n/a'}`\n"
        + ("- Blockers:\n" + "\n".join(f"  - {b}" for b in blockers) + "\n" if blockers else "")
        + (f"\n> {so.get('summary')}\n" if so.get("summary") else "")
    )


def _comment_pr_opened(gh: GitHubClient, devin: DevinClient, task: Task, policy: Policy) -> None:
    so = task.structured_output or {}
    verify = so.get("verification_commands_run") or []
    body = (
        f"✅ **Pull request opened:** {task.pr_url}\n\n"
        f"- Session: {task.session_url} · ACUs so far: `{task.acus_consumed:.2f}`\n"
        f"- Files changed: {', '.join('`' + f + '`' for f in (so.get('files_changed') or [])[:10]) or '_reported on the PR_'}\n"
        + (f"- Verification run: {', '.join('`' + v + '`' for v in verify)}\n" if verify else "")
        + f"- Trust tier: `{(policy.tier_for(task.issue_class).name if policy.tier_for(task.issue_class) else 'none')}`"
        f" — auto-merge requires CI green + clean Devin Review.\n"
    )
    gh.comment(task.issue_number, body)


def _merge_comment(task: Task, reason: str) -> str:
    return (
        f"🚢 **Merged automatically** — {reason}.\n\n"
        f"- PR: {task.pr_url}\n- Session: {task.session_url}\n"
        f"- ACUs for this task: `{task.acus_consumed:.2f}`"
    )

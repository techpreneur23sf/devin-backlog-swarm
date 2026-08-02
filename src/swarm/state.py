"""Ledger persistence on the orphan `swarm-state` branch, plus rebuild.

Two runs of the reconciler can overlap (Actions retries, manual dispatch during
a cron tick). Writes are therefore compare-and-swap against the blob SHA and
retried after a re-read, and every mutation is idempotent so a lost race costs
one tick, never correctness.

`rebuild()` reconstructs the entire ledger from Devin session tags. It exists
because the honest answer to "what happens when your state gets corrupted?"
should be a command, not a shrug.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

from .devin import DevinClient
from .gh import GitHubClient
from .models import (
    DISPATCHED,
    FAILED,
    NEEDS_HUMAN,
    PR_OPEN,
    QUEUED,
    RUNNING,
    Ledger,
    Task,
)

STATE_BRANCH = os.environ.get("SWARM_STATE_BRANCH", "swarm-state")
STATE_PATH = "state.json"

_BRANCH_README = """# swarm-state

Machine-written task ledger for the Devin backlog swarm. `state.json` is the
detail store; GitHub issue labels carry coarse state; Devin session tags carry
the correlation key. Do not edit by hand — run `swarm state rebuild` instead.
"""


class StateStore:
    def __init__(self, gh: GitHubClient, branch: str = STATE_BRANCH) -> None:
        self.gh = gh
        self.branch = branch
        self._sha: str | None = None
        self._snapshot: dict[str, Any] = {}

    def ensure_branch(self) -> None:
        self.gh.create_orphan_branch(self.branch, _BRANCH_README)

    def load(self) -> Ledger:
        f = self.gh.get_file(STATE_PATH, self.branch)
        if not f:
            self._sha = None
            self._snapshot = {}
            return Ledger(repo=self.gh.repo)
        self._sha = f["sha"]
        data = json.loads(base64.b64decode(f["content"]).decode())
        ledger = Ledger.from_dict(data)
        self._snapshot = {k: json.dumps(t.to_dict(), sort_keys=True) for k, t in ledger.tasks.items()}
        return ledger

    def save(self, ledger: Ledger, message: str = "chore(swarm): update ledger") -> None:
        """Compare-and-swap write; on conflict re-read, re-apply, retry."""
        content = json.dumps(ledger.to_dict(), indent=2, sort_keys=True) + "\n"
        for _attempt in range(5):
            res = self.gh.put_file(STATE_PATH, self.branch, content, message, sha=self._sha)
            if isinstance(res, dict) and res.get("content"):
                self._sha = res["content"]["sha"]
                return
            # 409/422: someone else wrote first. Overlay only the tasks *this*
            # process actually changed — copying the whole in-memory ledger
            # would silently revert the winner's work on every other task,
            # which is exactly the lost update CAS is supposed to prevent.
            mine = {
                key: task
                for key, task in ledger.tasks.items()
                if json.dumps(task.to_dict(), sort_keys=True) != self._snapshot.get(key)
            }
            runs = dict(ledger.runs)
            fresh = self.load()
            fresh.tasks.update(mine)
            fresh.runs.update(runs)
            ledger = fresh
            content = json.dumps(ledger.to_dict(), indent=2, sort_keys=True) + "\n"
        raise RuntimeError("could not write ledger after 5 attempts (persistent write conflict)")


# -- rebuild ------------------------------------------------------------------
_ISSUE_TAG = re.compile(r"^issue:(\d+)$")
_CLASS_TAG = re.compile(r"^class:(.+)$")


def rebuild(gh: GitHubClient, devin: DevinClient, repo_tag: str | None = None) -> Ledger:
    """Rebuild the ledger from Devin session tags + live GitHub state."""
    ledger = Ledger(repo=gh.repo)
    tags = ["swarm"] + ([repo_tag] if repo_tag else [])
    sessions = devin.list_sessions(tags=tags)

    by_issue: dict[int, list[dict[str, Any]]] = {}
    for s in sessions:
        for tag in s.get("tags") or []:
            m = _ISSUE_TAG.match(tag)
            if m:
                by_issue.setdefault(int(m.group(1)), []).append(s)

    for issue_number, sess_list in by_issue.items():
        sess_list.sort(key=lambda s: s.get("created_at") or 0)
        latest = sess_list[-1]
        issue_class = "unclassified"
        for tag in latest.get("tags") or []:
            m = _CLASS_TAG.match(tag)
            if m:
                issue_class = m.group(1)
        task = Task(
            issue_number=issue_number,
            issue_class=issue_class,
            session_id=latest.get("session_id"),
            session_url=latest.get("url"),
            session_status=latest.get("status"),
            session_status_detail=latest.get("status_detail"),
            structured_output=latest.get("structured_output"),
            acus_consumed=sum(float(s.get("acus_consumed") or 0) for s in sess_list),
            attempts=len(sess_list),
            dispatched_at=latest.get("created_at"),
        )
        prs = latest.get("pull_requests") or []
        if prs:
            task.pr_url = prs[0].get("url") or prs[0].get("html_url")
            task.state = PR_OPEN
        else:
            task.state = _state_from_session(latest)
        ledger.upsert(task)

    # Issues labelled for the swarm that have no session yet are queued work.
    for issue in gh.list_issues(labels=["devin:auto"]):
        n = issue["number"]
        existing = ledger.get(n)
        labels = [lbl["name"] for lbl in issue.get("labels", [])]
        klass = next((lbl.split(":", 1)[1] for lbl in labels if lbl.startswith("class:")), "unclassified")
        if existing is None:
            ledger.upsert(Task(issue_number=n, issue_title=issue["title"], issue_class=klass, state=QUEUED))
        else:
            existing.issue_title = issue["title"]
            if existing.issue_class == "unclassified":
                existing.issue_class = klass
    return ledger


def _state_from_session(session: dict[str, Any]) -> str:
    status = session.get("status")
    detail = session.get("status_detail")
    if detail == "waiting_for_user":
        return NEEDS_HUMAN
    if status in ("new", "claimed"):
        return DISPATCHED
    if status in ("running", "resuming"):
        return RUNNING
    if status == "error":
        return FAILED
    if status in ("exit", "suspended"):
        return FAILED if detail == "error" else NEEDS_HUMAN
    return DISPATCHED

"""Task ledger data model.

The ledger is the only durable state the swarm keeps. It lives in `state.json`
on the orphan `swarm-state` branch of the target repo, and it can be rebuilt
from scratch from Devin session tags (`swarm state rebuild`).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# --- task states -------------------------------------------------------------
QUEUED = "queued"
DISPATCHED = "dispatched"
RUNNING = "running"
PR_OPEN = "pr_open"
REVIEW_PENDING = "review_pending"
REVIEW_CLEAN = "review_clean"
NEEDS_HUMAN = "needs_human"
FAILED = "failed"
MERGED = "merged"
ABANDONED = "abandoned"
WONT_FIX = "wont_fix"

TERMINAL_STATES = {MERGED, ABANDONED, WONT_FIX}
#: states in which the reconciler still has something to observe
ACTIVE_STATES = {DISPATCHED, RUNNING, PR_OPEN, REVIEW_PENDING, REVIEW_CLEAN}
#: states that occupy a concurrency slot / hold a touch scope
IN_FLIGHT_STATES = {DISPATCHED, RUNNING}

FAILURE_ENV_SETUP = "env_setup"
FAILURE_TESTS = "tests_failed"
FAILURE_AMBIGUOUS = "ambiguous_issue"
FAILURE_ACU_CAP = "acu_cap"
FAILURE_CONFLICT = "conflict"
FAILURE_SESSION_ERROR = "session_error"
FAILURE_NO_PR = "no_pr"


def now() -> int:
    return int(time.time())


@dataclass
class Task:
    issue_number: int
    issue_title: str = ""
    issue_class: str = "unclassified"
    touch_scope: list[str] = field(default_factory=lambda: ["**"])
    state: str = QUEUED
    session_id: str | None = None
    session_url: str | None = None
    session_status: str | None = None
    session_status_detail: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    pr_state: str | None = None
    #: "swarm" when the merge policy merged it, "human" when someone else did
    merged_by: str | None = None
    ci_status: str | None = None
    review_status: str | None = None
    structured_output: dict[str, Any] | None = None
    acus_consumed: float = 0.0
    attempts: int = 0
    created_at: int = field(default_factory=now)
    dispatched_at: int | None = None
    pr_opened_at: int | None = None
    terminal_at: int | None = None
    failure_category: str | None = None
    last_error: str | None = None
    #: number of reconcile ticks observed in `waiting_for_user`
    waiting_ticks: int = 0
    #: True if the session ever reported waiting_for_user (autonomy metric)
    ever_waited_for_user: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)

    # -- helpers --------------------------------------------------------------
    def transition(self, new_state: str, reason: str = "") -> bool:
        """Move to `new_state`, recording the transition. No-op if unchanged."""
        if self.state == new_state:
            return False
        self.history.append(
            {"at": now(), "from": self.state, "to": new_state, "reason": reason}
        )
        self.state = new_state
        if new_state in TERMINAL_STATES or new_state in (FAILED, NEEDS_HUMAN):
            self.terminal_at = now()
        return True

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def needed_a_human(self) -> bool:
        """Was a person actually pulled into this task?

        Not the same as `ever_waited_for_user`: a session that finishes its work
        sits in `waiting_for_user` because nobody is talking to it, which says
        nothing about whether anyone had to. A person was pulled in when the
        task was parked for one (`needs_human`) or when one merged the PR.
        """
        return self.merged_by == "human" or any(h.get("to") == NEEDS_HUMAN for h in self.history)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        task = cls(**{k: v for k, v in data.items() if k in known})
        if task.state == MERGED and task.merged_by is None:
            task.merged_by = _merge_actor_from_history(task.history)
        return task


def _merge_actor_from_history(history: list[dict[str, Any]]) -> str | None:
    """Who merged a task recorded before `merged_by` existed.

    Read from the transition the reconciler wrote at the time, so the answer is
    still evidence rather than an assumption; entries that say neither stay
    unknown and are counted as neither.
    """
    for entry in reversed(history):
        if entry.get("to") != MERGED:
            continue
        reason = (entry.get("reason") or "").lower()
        if "human" in reason:
            return "human"
        if reason.startswith("satisfies tier"):
            return "swarm"
        return None
    return None


@dataclass
class Ledger:
    schema_version: int = SCHEMA_VERSION
    updated_at: int = field(default_factory=now)
    repo: str = ""
    tasks: dict[str, Task] = field(default_factory=dict)
    #: run_id -> {started_at, acus_budgeted, dispatched:[issue numbers]}
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, issue_number: int) -> Task | None:
        return self.tasks.get(str(issue_number))

    def upsert(self, task: Task) -> Task:
        self.tasks[str(task.issue_number)] = task
        return task

    def active(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.state in ACTIVE_STATES]

    def in_flight(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.state in IN_FLIGHT_STATES]

    def queued(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.state == QUEUED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": now(),
            "repo": self.repo,
            "tasks": {k: v.to_dict() for k, v in sorted(self.tasks.items(), key=lambda kv: int(kv[0]))},
            "runs": self.runs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Ledger:
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            updated_at=data.get("updated_at", now()),
            repo=data.get("repo", ""),
            tasks={k: Task.from_dict(v) for k, v in (data.get("tasks") or {}).items()},
            runs=data.get("runs") or {},
        )

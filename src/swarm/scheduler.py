"""Conflict-aware fan-out.

Two sessions editing the same files produce conflicting PRs and waste ACUs, so
the scheduler is a greedy set-packing pass over declared touch scopes: dispatch
a queued task only if its scope is disjoint from every scope currently held by
an in-flight task, and only while a concurrency slot and the ACU budget remain.

Scope overlap is decided on glob prefixes rather than file lists because the
files a session will touch are not knowable before it runs. Overlap is
therefore conservative: a false positive costs a tick of latency, a false
negative costs a merge conflict.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .models import Ledger, Task, now
from .policy import Policy


def _static_prefix(glob: str) -> str:
    out: list[str] = []
    for part in glob.split("/"):
        if any(ch in part for ch in "*?[]"):
            break
        out.append(part)
    return "/".join(out)


def scopes_overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    for ga in a:
        pa = _static_prefix(ga)
        for gb in b:
            pb = _static_prefix(gb)
            if pa == "" or pb == "":  # a "**" scope serialises against everything
                return True
            if pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/"):
                return True
    return False


def reserved_acus_today(ledger: Ledger, policy: Policy, now_ts: int | None = None) -> float:
    """What today's dispatches were allowed to spend, whether or not it is metered.

    The daily cap has to hold on an account Devin does not meter in ACUs, where
    `GET /consumption/daily` reports 0.0 forever: a cap compared against zero
    never binds, and "unlimited" is the one budget nobody asked for. So each
    dispatch reserves its class's per-session limit up front, and the scheduler
    spends the larger of reserved and observed — reservations bound the blast
    radius, observation corrects it downwards once a metered plan reports back.
    """
    midnight = ((now_ts or now()) // 86400) * 86400
    return sum(
        max(t.acus_consumed, float(policy.acu_limit(t.issue_class)))
        for t in ledger.dispatched_since(midnight)
    )


def plan(
    queued: Iterable[Task],
    in_flight: Iterable[Task],
    policy: Policy,
    acus_spent_today: float,
    holding_scope: Iterable[Task] = (),
) -> tuple[list[Task], list[tuple[Task, str]]]:
    """Return (tasks to dispatch, [(task, skip reason)]).

    Two different resources are being rationed. `in_flight` tasks occupy a
    concurrency slot *and* hold their scope; `holding_scope` tasks — typically
    ones whose PR is open but unmerged — hold their scope without occupying a
    slot, because a second session editing those files would conflict with a
    branch that has not landed yet.
    """
    dispatch: list[Task] = []
    skipped: list[tuple[Task, str]] = []

    if policy.kill_switch:
        return [], [(t, "kill switch engaged") for t in queued]

    running = list(in_flight)
    held: list[list[str]] = [list(t.touch_scope) for t in running + list(holding_scope)]
    slots = policy.budget.max_concurrent_sessions - len(running)
    budget_left = policy.budget.daily_acu_cap - acus_spent_today

    for task in sorted(queued, key=lambda t: (policy.priority(t.issue_class), t.issue_number)):
        if task.attempts >= policy.budget.max_attempts_per_issue:
            skipped.append((task, f"attempts exhausted ({task.attempts})"))
            continue
        if slots <= 0:
            skipped.append((task, "max_concurrent_sessions reached"))
            continue
        cost = policy.acu_limit(task.issue_class)
        if cost > budget_left:
            skipped.append((task, f"daily ACU cap would be exceeded ({budget_left:.1f} left, needs {cost})"))
            continue
        scope = list(task.touch_scope)
        if any(scopes_overlap(scope, h) for h in held):
            skipped.append((task, "touch scope conflicts with a task already in flight"))
            continue
        dispatch.append(task)
        held.append(scope)
        slots -= 1
        budget_left -= cost

    return dispatch, skipped

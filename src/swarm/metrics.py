"""Metrics that answer "how would an engineering leader know this is working?".

Deliberately not task counts. Every number here is derived from the ledger,
which is itself derived from API responses; nothing is estimated and nothing is
rounded in a flattering direction. When there is no data, the value is None and
the dashboard renders "—" rather than a zero that looks like a result.
"""

from __future__ import annotations

import statistics
from typing import Any

from .models import (
    ABANDONED,
    FAILED,
    MERGED,
    NEEDS_HUMAN,
    WONT_FIX,
    Ledger,
    Task,
)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def compute(ledger: Ledger) -> dict[str, Any]:
    tasks = list(ledger.tasks.values())
    merged = [t for t in tasks if t.state == MERGED]
    with_pr = [t for t in tasks if t.pr_url]
    [t for t in tasks if t.state in (MERGED, ABANDONED, WONT_FIX, FAILED, NEEDS_HUMAN)]
    dispatched = [t for t in tasks if t.session_id]

    acus_total = sum(t.acus_consumed for t in tasks)
    acus_merged = sum(t.acus_consumed for t in merged)
    # Whether Devin meters this account in ACUs at all. Self-serve plans bill
    # included quota then on-demand credits, and the API reports neither per
    # session, so `acus_consumed` is 0.0 on every session. Reporting cost per
    # merge as "0.0 ACUs" would be a lie in the flattering direction; the
    # dashboard says so and falls back to the effort signals below.
    acus_metered = acus_total > 0

    #: Devin's own size classification, which is populated on any plan.
    sizes: dict[str, int] = {}
    for t in dispatched:
        if t.session_size:
            sizes[t.session_size] = sizes.get(t.session_size, 0) + 1
    session_hours = [
        (t.terminal_at - t.dispatched_at) / 3600.0
        for t in dispatched
        if t.terminal_at and t.dispatched_at and t.terminal_at >= t.dispatched_at
    ]

    lead_times = [
        (t.pr_opened_at - t.created_at) / 3600.0
        for t in with_pr
        if t.pr_opened_at and t.created_at and t.pr_opened_at >= t.created_at
    ]

    # Autonomy is measured over work that produced something reviewable: of the
    # PRs the swarm opened, what fraction landed without a human being pulled in?
    # Counting a failed task as "autonomous" because nobody was asked would
    # flatter the number. Who pressed merge and whether the session got there
    # without pulling a person in are separate questions: a PR a person merged
    # is a landed change, but it is not autonomy.
    merged_by_swarm = [t for t in merged if t.merged_by == "swarm"]
    merged_by_human = [t for t in merged if t.merged_by == "human"]
    autonomous = [t for t in merged_by_swarm if not t.needed_a_human]

    by_class: dict[str, dict[str, Any]] = {}
    for t in tasks:
        b = by_class.setdefault(t.issue_class, {"total": 0, "merged": 0, "pr": 0, "acus": 0.0})
        b["total"] += 1
        b["acus"] += t.acus_consumed
        if t.pr_url:
            b["pr"] += 1
        if t.state == MERGED:
            b["merged"] += 1
    for b in by_class.values():
        b["merge_rate"] = (b["merged"] / b["total"]) if b["total"] else 0.0

    failures: dict[str, int] = {}
    for t in tasks:
        if t.failure_category:
            failures[t.failure_category] = failures.get(t.failure_category, 0) + 1

    return {
        "counts": {
            "tasks": len(tasks),
            "dispatched": len(dispatched),
            "prs_opened": len(with_pr),
            "merged": len(merged),
            "merged_by_swarm": len(merged_by_swarm),
            "merged_by_human": len(merged_by_human),
            "merged_without_human_input": len(autonomous),
            "needs_human": len([t for t in tasks if t.state == NEEDS_HUMAN]),
            "failed": len([t for t in tasks if t.state == FAILED]),
            "abandoned": len([t for t in tasks if t.state == ABANDONED]),
            "wont_fix": len([t for t in tasks if t.state == WONT_FIX]),
            "in_flight": len(ledger.in_flight()),
            "queued": len(ledger.queued()),
        },
        "acus_metered": acus_metered,
        "acus_total": round(acus_total, 2),
        "effort": {
            "session_sizes": dict(sorted(sizes.items())),
            "devin_messages_total": sum(t.devin_messages for t in dispatched),
            "median_session_hours": round(_median(session_hours), 2) if session_hours else None,
            "median_session_hours_merged": _median_hours(merged),
        },
        # Total spend over merges, not spend on the merged tasks alone: the cost
        # of a landed change includes the attempts that never landed.
        # None, not 0.0, when the account is not metered in ACUs: an unreported
        # cost is missing data, and rendering it as free is the one rounding
        # error a buyer would never forgive.
        "acus_per_merged_pr": round(acus_total / len(merged), 2) if merged and acus_metered else None,
        "acus_on_merged_tasks": round(acus_merged, 2) if acus_metered else None,
        "acus_per_pr_opened": (
            round(sum(t.acus_consumed for t in with_pr) / len(with_pr), 2) if with_pr and acus_metered else None
        ),
        "median_issue_to_pr_hours": round(_median(lead_times), 2) if lead_times else None,
        "pr_rate": round(len(with_pr) / len(dispatched), 3) if dispatched else None,
        "merge_rate": round(len(merged) / len(dispatched), 3) if dispatched else None,
        "autonomy_rate": round(len(autonomous) / len(with_pr), 3) if with_pr else None,
        "by_class": by_class,
        "failures": failures,
        "states": _state_histogram(tasks),
    }


def _usage_phrase(m: dict[str, Any]) -> str:
    """Say what the account actually meters rather than printing 0.0 ACUs."""
    if m["acus_metered"]:
        return f"**{m['acus_total']}** ACUs spent"
    hours = m["effort"]["median_session_hours"]
    tail = f"median session **{hours}h**" if hours else "no completed sessions yet"
    return f"ACUs not metered on this account — {tail}"


def _median_hours(tasks: list[Task]) -> float | None:
    hours = [
        (t.terminal_at - t.dispatched_at) / 3600.0
        for t in tasks
        if t.terminal_at and t.dispatched_at and t.terminal_at >= t.dispatched_at
    ]
    return round(_median(hours), 2) if hours else None


def _state_histogram(tasks: list[Task]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tasks:
        out[t.state] = out.get(t.state, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def step_summary(ledger: Ledger, log: list[str], title: str = "Swarm reconcile") -> str:
    """Markdown for $GITHUB_STEP_SUMMARY — the first thing an engineer clicks."""
    m = compute(ledger)
    c = m["counts"]
    lines = [
        f"## {title}",
        "",
        f"**{c['tasks']}** tasks · **{c['in_flight']}** in flight · **{c['prs_opened']}** PRs · "
        f"**{c['merged']}** merged ({c['merged_by_swarm']} by the swarm, "
        f"{c['merged_by_human']} by a human) · **{c['needs_human']}** need a human · "
        f"{_usage_phrase(m)}",
        "",
        "| Issue | Class | State | PR | CI | Review | Session | Devin msgs |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in sorted(ledger.tasks.values(), key=lambda t: t.issue_number):
        pr = f"[#{t.pr_number}]({t.pr_url})" if t.pr_url else "—"
        lines.append(
            f"| #{t.issue_number} | `{t.issue_class}` | `{t.state}` | {pr} | "
            f"{t.ci_status or '—'} | {t.review_status or '—'} | {t.session_size or '—'} | "
            f"{t.devin_messages or '—'} |"
        )
    if log:
        lines += ["", "### Transitions this tick", ""] + [f"- {line}" for line in log]
    else:
        lines += ["", "_No state changes this tick._"]
    return "\n".join(lines) + "\n"

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

    lead_times = [
        (t.pr_opened_at - t.created_at) / 3600.0
        for t in with_pr
        if t.pr_opened_at and t.created_at and t.pr_opened_at >= t.created_at
    ]

    # Autonomy is measured over work that produced something reviewable: of the
    # PRs the swarm opened, what fraction landed without a human being pulled in?
    # Counting a failed task as "autonomous" because nobody was asked would
    # flatter the number.
    # A PR a person merged is a landed change, but it is not autonomy.
    autonomous = [t for t in merged if t.merged_by != "human" and not t.ever_waited_for_user]
    merged_by_human = [t for t in merged if t.merged_by == "human"]

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
            "merged_autonomously": len(autonomous),
            "merged_by_human": len(merged_by_human),
            "needs_human": len([t for t in tasks if t.state == NEEDS_HUMAN]),
            "failed": len([t for t in tasks if t.state == FAILED]),
            "abandoned": len([t for t in tasks if t.state == ABANDONED]),
            "wont_fix": len([t for t in tasks if t.state == WONT_FIX]),
            "in_flight": len(ledger.in_flight()),
            "queued": len(ledger.queued()),
        },
        "acus_total": round(acus_total, 2),
        # Total spend over merges, not spend on the merged tasks alone: the cost
        # of a landed change includes the attempts that never landed.
        "acus_per_merged_pr": round(acus_total / len(merged), 2) if merged else None,
        "acus_on_merged_tasks": round(acus_merged, 2),
        "acus_per_pr_opened": round(sum(t.acus_consumed for t in with_pr) / len(with_pr), 2) if with_pr else None,
        "median_issue_to_pr_hours": round(_median(lead_times), 2) if lead_times else None,
        "pr_rate": round(len(with_pr) / len(dispatched), 3) if dispatched else None,
        "merge_rate": round(len(merged) / len(dispatched), 3) if dispatched else None,
        "autonomy_rate": round(len(autonomous) / len(with_pr), 3) if with_pr else None,
        "by_class": by_class,
        "failures": failures,
        "states": _state_histogram(tasks),
    }


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
        f"**{c['merged']}** merged ({c['merged_autonomously']} autonomously, "
        f"{c['merged_by_human']} by a human) · **{c['needs_human']}** need a human · "
        f"**{m['acus_total']}** ACUs spent",
        "",
        "| Issue | Class | State | PR | CI | Review | ACUs |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in sorted(ledger.tasks.values(), key=lambda t: t.issue_number):
        pr = f"[#{t.pr_number}]({t.pr_url})" if t.pr_url else "—"
        lines.append(
            f"| #{t.issue_number} | `{t.issue_class}` | `{t.state}` | {pr} | "
            f"{t.ci_status or '—'} | {t.review_status or '—'} | {t.acus_consumed:.1f} |"
        )
    if log:
        lines += ["", "### Transitions this tick", ""] + [f"- {line}" for line in log]
    else:
        lines += ["", "_No state changes this tick._"]
    return "\n".join(lines) + "\n"

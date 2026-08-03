"""Static dashboard generation.

No server, no database, no build step: one HTML file plus the raw `state.json`
and `metrics.json` it was rendered from, committed to `gh-pages`. Every number
on the page is traceable to an API response, and the page says so by shipping
the data it was rendered from next to it.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .metrics import compute
from .models import MERGED, Ledger

_STATE_COLOURS = {
    "queued": "#94a3b8",
    "dispatched": "#38bdf8",
    "running": "#0ea5e9",
    "pr_open": "#a78bfa",
    "review_pending": "#c084fc",
    "review_clean": "#818cf8",
    "merged": "#22c55e",
    "needs_human": "#f59e0b",
    "failed": "#ef4444",
    "abandoned": "#6b7280",
    "wont_fix": "#6b7280",
}


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def _pct(value: Any) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def render(ledger: Ledger, repo: str, run_context: dict[str, Any] | None = None) -> str:
    m = compute(ledger)
    c = m["counts"]
    ctx = run_context or {}
    generated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    effort = m["effort"]
    sizes = ", ".join(f"{n}×{k}" for k, n in effort["session_sizes"].items()) or "—"
    cost_card = (
        ("ACUs per merged PR", _fmt(m["acus_per_merged_pr"]), "unit cost of shipped work")
        if m["acus_metered"]
        # An account Devin does not meter in ACUs reports 0.0 for every session,
        # so there is no unit cost to show. Showing "0.0 ACUs" would read as free.
        else ("Cost per merged PR", "not metered", "this plan bills usage, not ACUs — see below")
    )
    cards = [
        cost_card,
        ("Median issue → PR", _fmt(m["median_issue_to_pr_hours"], " h"), "against a backlog measured in weeks"),
        ("Autonomy rate", _pct(m["autonomy_rate"]), "finished with zero human input"),
        ("Merge rate", _pct(m["merge_rate"]), "of dispatched sessions"),
        ("PRs opened", str(c["prs_opened"]), f"{c['merged']} merged, {c['needs_human']} awaiting a human"),
        (
            "Median session",
            _fmt(effort["median_session_hours"], " h"),
            f"dispatch → finish · sizes {sizes}",
        ),
    ]
    card_html = "\n".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div><div class="s">{s}</div></div>'
        for k, v, s in cards
    )

    rows: list[str] = []
    for t in sorted(ledger.tasks.values(), key=lambda t: t.issue_number):
        colour = _STATE_COLOURS.get(t.state, "#94a3b8")
        pr = f'<a href="{t.pr_url}">#{t.pr_number}</a>' if t.pr_url else "—"
        sess = f'<a href="{t.session_url}">session</a>' if t.session_url else "—"
        so = t.structured_output or {}
        rows.append(
            f"<tr>"
            f'<td><a href="https://github.com/{repo}/issues/{t.issue_number}">#{t.issue_number}</a></td>'
            f'<td class="title">{_esc(t.issue_title)}</td>'
            f"<td><code>{t.issue_class}</code></td>"
            f'<td><span class="pill" style="background:{colour}">{t.state}</span></td>'
            f"<td>{pr}</td><td>{t.ci_status or '—'}</td><td>{t.review_status or '—'}</td>"
            f"<td>{so.get('confidence') or '—'}</td>"
            f"<td>{t.session_size or '—'}</td><td>{t.devin_messages or '—'}</td><td>{sess}</td>"
            f"</tr>"
        )

    class_rows = "\n".join(
        f"<tr><td><code>{k}</code></td><td>{v['total']}</td><td>{v['pr']}</td><td>{v['merged']}</td>"
        f"<td>{v['merge_rate'] * 100:.0f}%</td></tr>"
        for k, v in sorted(m["by_class"].items())
    )
    failure_rows = "\n".join(
        f"<tr><td><code>{k}</code></td><td>{v}</td></tr>" for k, v in sorted(m["failures"].items())
    ) or '<tr><td colspan="2">No failures recorded.</td></tr>'

    burn = _burndown_svg(ledger)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backlog swarm — {repo}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; background:#0b1020; color:#e2e8f0; font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
header {{ padding:28px 32px 8px; border-bottom:1px solid #1e293b; }}
h1 {{ margin:0 0 4px; font-size:20px; }}
.sub {{ color:#94a3b8; font-size:13px; }}
main {{ padding:24px 32px 64px; max-width:1180px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; margin:20px 0 28px; }}
.card {{ background:#111827; border:1px solid #1f2937; border-radius:10px; padding:14px 16px; }}
.card .k {{ color:#94a3b8; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
.card .v {{ font-size:26px; font-weight:600; margin:6px 0 2px; }}
.card .s {{ color:#64748b; font-size:12px; }}
h2 {{ font-size:15px; margin:28px 0 10px; color:#cbd5e1; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #1e293b; vertical-align:top; }}
th {{ color:#94a3b8; font-weight:500; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }}
td.title {{ max-width:380px; }}
code {{ background:#1e293b; padding:1px 5px; border-radius:4px; font-size:12px; }}
.pill {{ display:inline-block; padding:2px 8px; border-radius:999px; color:#0b1020; font-weight:600; font-size:11px; }}
a {{ color:#7dd3fc; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
footer {{ color:#64748b; font-size:12px; margin-top:32px; border-top:1px solid #1e293b; padding-top:14px; }}
</style></head>
<body>
<header>
  <h1>Autonomous backlog swarm — <a href="https://github.com/{repo}">{repo}</a></h1>
  <div class="sub">Generated {generated} by the reconciler. Every figure is derived from the Devin and GitHub
  APIs; the source data is published next to this page as
  <a href="state.json">state.json</a> and <a href="metrics.json">metrics.json</a>.</div>
</header>
<main>
  <div class="cards">{card_html}</div>

  <h2>Backlog burndown</h2>
  {burn}

  <h2>Tasks</h2>
  <table><thead><tr><th>Issue</th><th>Title</th><th>Class</th><th>State</th><th>PR</th><th>CI</th>
  <th>Review</th><th>Confidence</th><th>Size</th><th>Devin msgs</th><th>Session</th></tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan="11">No tasks yet.</td></tr>'}</tbody></table>

  <h2>Cost</h2>
  <p class="sub">{_cost_note(m)}</p>

  <h2>By issue class</h2>
  <table><thead><tr><th>Class</th><th>Tasks</th><th>PRs</th><th>Merged</th><th>Merge rate</th></tr></thead>
  <tbody>{class_rows or '<tr><td colspan="5">—</td></tr>'}</tbody></table>

  <h2>Failure taxonomy</h2>
  <p class="sub">Separates "the agent is bad" from "our configuration is bad" — usually the latter.</p>
  <table><thead><tr><th>Category</th><th>Tasks</th></tr></thead><tbody>{failure_rows}</tbody></table>

  <footer>
    Reconciler run <code>{_esc(str(ctx.get('run_id', 'local')))}</code> ·
    ledger schema v{ledger.schema_version} ·
    {c['tasks']} tasks tracked · no server, no database: this page is a file on the
    <code>gh-pages</code> branch.
  </footer>
</main></body></html>
"""


def _cost_note(m: dict) -> str:
    """Say which unit the account is billed in rather than inventing one."""
    if m["acus_metered"]:
        return (
            f"{m['acus_total']} ACUs across every session, {m['acus_per_merged_pr']} per merged PR — "
            "total spend divided by merges, so failed attempts are charged to the changes that landed."
        )
    return (
        "<code>acus_consumed</code> is <code>0.0</code> on every session here and "
        "<code>GET /consumption/daily</code> reports <code>total_acus: 0.0</code>: ACUs are the "
        "Enterprise billing unit, and this account is billed for usage as included quota plus "
        "on-demand credits, which the API does not expose per session. Rather than print a "
        "fabricated unit cost, effort is reported in the units the API does return — Devin's own "
        f"session size classification ({', '.join(f'{n}×{k}' for k, n in m['effort']['session_sizes'].items()) or '—'}), "
        f"{m['effort']['devin_messages_total']} Devin messages, and wall-clock session time. "
        "The daily budget cap still binds: each dispatch reserves its class's per-session ACU "
        "limit up front instead of trusting an unmetered zero."
    )


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _burndown_svg(ledger: Ledger, width: int = 1100, height: int = 120) -> str:
    """Cumulative merged-vs-open over time, from ledger timestamps only."""
    events: list[tuple] = []
    for t in ledger.tasks.values():
        events.append((t.created_at, 1))
        if t.state == MERGED and t.terminal_at:
            events.append((t.terminal_at, -1))
    if len(events) < 2:
        return '<p class="sub">Not enough history yet to draw a burndown.</p>'
    events.sort()
    t0, t1 = events[0][0], max(e[0] for e in events)
    span = max(1, t1 - t0)
    open_count = 0
    points = []
    peak = 1
    for ts, delta in events:
        open_count += delta
        peak = max(peak, open_count)
        points.append((ts, open_count))
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{(ts - t0) / span * (width - 20) + 10:.1f},"
        f"{height - 10 - (val / peak) * (height - 30):.1f}"
        for i, (ts, val) in enumerate(points)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
        f'aria-label="open swarm tasks over time">'
        f'<path d="{path}" fill="none" stroke="#38bdf8" stroke-width="2"/>'
        f'<text x="10" y="14" fill="#64748b" font-size="11">open swarm tasks (peak {peak})</text>'
        f"</svg>"
    )


def render_metrics_json(ledger: Ledger) -> str:
    return json.dumps(compute(ledger), indent=2, sort_keys=True) + "\n"

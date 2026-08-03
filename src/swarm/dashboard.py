"""Static dashboard generation.

No server, no database, no build step: one HTML file plus the raw `state.json`
and `metrics.json` it was rendered from, committed to `gh-pages`. Every number
on the page is traceable to an API response, and the page says so by shipping
the data it was rendered from next to it.

A metric with no data is not rendered. A card reading "not metered" occupies the
space of a result while carrying none, so the unavailable ones are omitted and
explained once, in prose, at the bottom.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .metrics import compute
from .models import MERGED, Ledger

#: state -> (text, background, border). Light, low-saturation, readable at 11px.
_STATE_STYLE = {
    "queued": ("#475569", "#f1f5f9", "#e2e8f0"),
    "dispatched": ("#0369a1", "#e0f2fe", "#bae6fd"),
    "running": ("#0369a1", "#e0f2fe", "#bae6fd"),
    "pr_open": ("#6d28d9", "#f3e8ff", "#e9d5ff"),
    "review_pending": ("#7c3aed", "#f5f3ff", "#ddd6fe"),
    "review_clean": ("#4338ca", "#eef2ff", "#e0e7ff"),
    "merged": ("#15803d", "#dcfce7", "#bbf7d0"),
    "needs_human": ("#b45309", "#fef3c7", "#fde68a"),
    "failed": ("#b91c1c", "#fee2e2", "#fecaca"),
    "abandoned": ("#475569", "#f1f5f9", "#e2e8f0"),
    "wont_fix": ("#475569", "#f1f5f9", "#e2e8f0"),
}

#: An absent value, so a blank cell is deliberate rather than a rendering bug.
_DASH = '<span class="muted">—</span>'

#: signal -> dot colour. Anything unknown is grey, never green.
_DOT = {
    "green": "#16a34a",
    "clean": "#16a34a",
    "red": "#dc2626",
    "failing": "#dc2626",
    "comments": "#d97706",
    "pending": "#cbd5e1",
    "none": "#cbd5e1",
}


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def _pct(value: Any) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _dot(value: str | None, label: str | None = None) -> str:
    """A status as a dot plus its word, so it scans at a glance and still reads."""
    if not value:
        return _DASH
    colour = _DOT.get(value, "#94a3b8")
    text = label or value.replace("_", " ")
    return f'<span class="dot" style="background:{colour}"></span><span class="dot-l">{text}</span>'


def _state_pill(state: str) -> str:
    fg, bg, border = _STATE_STYLE.get(state, ("#475569", "#f1f5f9", "#e2e8f0"))
    return (
        f'<span class="pill" style="color:{fg};background:{bg};border-color:{border}">{state.replace("_", " ")}</span>'
    )


def render(ledger: Ledger, repo: str, run_context: dict[str, Any] | None = None) -> str:
    m = compute(ledger)
    c = m["counts"]
    ctx = run_context or {}
    generated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    effort = m["effort"]

    # Only metrics with data get a card. An unavailable unit cost is explained
    # once in "How this is measured", not printed where a number should be.
    cards: list[tuple[str, str, str]] = []
    if m["acus_metered"]:
        cards.append(("ACUs per merged PR", _fmt(m["acus_per_merged_pr"]), "unit cost of shipped work"))
    # Merges recorded before the ledger tracked an actor stay unattributed
    # rather than being counted for the side that flatters the number.
    unattributed = c["merged"] - c["merged_by_swarm"] - c["merged_by_human"]
    attribution = f"{c['merged_by_swarm']} by the swarm · {c['merged_by_human']} by a human"
    if unattributed > 0:
        attribution += f" · {unattributed} unattributed"
    cards += [
        ("Merged", str(c["merged"]), attribution),
        ("Autonomy rate", _pct(m["autonomy_rate"]), "of PRs opened, landed with no human input"),
        ("Merge rate", _pct(m["merge_rate"]), "of dispatched sessions"),
        ("Awaiting a human", str(c["needs_human"]), "parked by policy or by review"),
    ]
    if m["median_issue_to_pr_hours"] is not None:
        cards.append(
            ("Median issue → PR", _fmt(m["median_issue_to_pr_hours"], " h"), "against a backlog measured in weeks")
        )
    if effort["median_session_hours"] is not None:
        sizes = ", ".join(f"{n}×{k}" for k, n in effort["session_sizes"].items())
        cards.append(
            (
                "Median session",
                _fmt(effort["median_session_hours"], " h"),
                f"dispatch → finish{f' · {sizes}' if sizes else ''}",
            )
        )

    card_html = "\n".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div><div class="s">{s}</div></div>'
        for k, v, s in cards
    )

    rows: list[str] = []
    states_present: dict[str, int] = {}
    for t in sorted(ledger.tasks.values(), key=lambda t: t.issue_number):
        states_present[t.state] = states_present.get(t.state, 0) + 1
        so = t.structured_output or {}
        pr = f'<a href="{t.pr_url}">#{t.pr_number}</a>' if t.pr_url else _DASH
        sess = f'<a class="ext" href="{t.session_url}" title="Devin session">session ↗</a>' if t.session_url else _DASH
        conf = so.get("confidence")
        conf_html = f'<span class="conf conf-{conf}">{conf}</span>' if conf else _DASH
        haystack = f"{t.issue_number} {t.issue_title} {t.issue_class} {t.state}".lower()
        rows.append(
            f'<tr data-state="{t.state}" data-q="{_esc(haystack)}">'
            f'<td class="num"><a href="https://github.com/{repo}/issues/{t.issue_number}">#{t.issue_number}</a></td>'
            f'<td class="title" title="{_esc(t.issue_title)}">{_esc(t.issue_title)}</td>'
            f'<td><span class="tag">{t.issue_class}</span></td>'
            f"<td>{_state_pill(t.state)}</td>"
            f'<td class="num">{pr}</td>'
            f"<td>{_dot(t.ci_status)}</td>"
            f"<td>{_dot(t.review_status)}</td>"
            f"<td>{conf_html}</td>"
            f'<td class="num">{t.session_size or _DASH}</td>'
            f'<td class="num">{t.devin_messages or _DASH}</td>'
            f"<td>{sess}</td></tr>"
        )

    chips = "".join(
        f'<button class="chip" data-filter="{s}">{s.replace("_", " ")}<span class="ct">{n}</span></button>'
        for s, n in sorted(states_present.items(), key=lambda kv: -kv[1])
    )

    class_rows = "\n".join(
        f'<tr><td><span class="tag">{k}</span></td><td class="num">{v["total"]}</td>'
        f'<td class="num">{v["pr"]}</td><td class="num">{v["merged"]}</td>'
        f"<td>{_bar(v['merge_rate'])}</td></tr>"
        for k, v in sorted(m["by_class"].items())
    )
    failure_rows = (
        "\n".join(
            f'<tr><td><span class="tag">{k}</span></td><td class="num">{v}</td></tr>'
            for k, v in sorted(m["failures"].items())
        )
        or '<tr><td colspan="2" class="muted">No failures recorded.</td></tr>'
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backlog swarm — {repo}</title>
<style>
:root {{
  color-scheme: light;
  --bg:#f7f8fa; --panel:#fff; --line:#e8eaee; --line-2:#f1f2f5;
  --ink:#111827; --ink-2:#4b5563; --ink-3:#8a94a6; --accent:#2563eb;
  --shadow:0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.04);
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.55 "Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased; }}
a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.muted {{ color:var(--ink-3); }}
header {{ background:var(--panel); border-bottom:1px solid var(--line); }}
.wrap {{ max-width:1240px; margin:0 auto; padding:0 28px; }}
.hd {{ display:flex; align-items:flex-start; justify-content:space-between; gap:24px; padding:22px 0 20px; flex-wrap:wrap; }}
h1 {{ margin:0; font-size:19px; font-weight:650; letter-spacing:-.01em; }}
.sub {{ color:var(--ink-2); font-size:13px; margin-top:4px; max-width:640px; }}
.live {{ display:inline-flex; align-items:center; gap:7px; background:#f0fdf4; color:#15803d;
  border:1px solid #bbf7d0; border-radius:999px; padding:4px 11px; font-size:12px; font-weight:550; white-space:nowrap; }}
.live .dot {{ width:7px; height:7px; }}
main {{ padding:24px 0 72px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(196px,1fr)); gap:14px; margin:4px 0 26px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px; box-shadow:var(--shadow); }}
.card .k {{ color:var(--ink-2); font-size:12px; font-weight:550; }}
.card .v {{ font-size:28px; font-weight:640; letter-spacing:-.02em; margin:8px 0 4px; }}
.card .s {{ color:var(--ink-3); font-size:12px; line-height:1.45; }}
section {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; box-shadow:var(--shadow); margin-bottom:22px; overflow:hidden; }}
.sh {{ display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 18px; border-bottom:1px solid var(--line-2); flex-wrap:wrap; }}
.sh h2 {{ margin:0; font-size:14px; font-weight:600; }}
.sh p {{ margin:2px 0 0; font-size:12.5px; color:var(--ink-3); }}
.tools {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
input[type=search] {{ font:inherit; font-size:13px; padding:6px 11px; border:1px solid var(--line);
  border-radius:8px; background:var(--bg); min-width:210px; color:var(--ink); }}
input[type=search]:focus {{ outline:2px solid #dbeafe; border-color:#93c5fd; }}
.chip {{ font:inherit; font-size:12px; padding:5px 10px; border:1px solid var(--line); background:var(--panel);
  border-radius:999px; cursor:pointer; color:var(--ink-2); display:inline-flex; gap:6px; align-items:center; }}
.chip:hover {{ background:var(--bg); }}
.chip[aria-pressed=true] {{ background:#eff6ff; border-color:#bfdbfe; color:#1d4ed8; }}
.chip .ct {{ color:var(--ink-3); font-variant-numeric:tabular-nums; }}
.chip[aria-pressed=true] .ct {{ color:#3b82f6; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ text-align:left; padding:10px 14px; border-bottom:1px solid var(--line-2); vertical-align:middle; }}
tbody tr:last-child td {{ border-bottom:none; }}
tbody tr:hover {{ background:#fafbfc; }}
th {{ color:var(--ink-2); font-weight:550; font-size:12px; background:#fcfcfd; position:sticky; top:0; z-index:1;
  border-bottom:1px solid var(--line); white-space:nowrap; }}
th.sortable {{ cursor:pointer; user-select:none; }}
th.sortable:hover {{ color:var(--ink); }}
th.sortable::after {{ content:"↕"; opacity:.28; margin-left:6px; font-size:10px; }}
th[data-dir=asc]::after {{ content:"↑"; opacity:.9; }}
th[data-dir=desc]::after {{ content:"↓"; opacity:.9; }}
td.num, th.num {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.title {{ max-width:340px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.tag {{ display:inline-block; padding:2px 8px; border-radius:6px; background:#f1f5f9; color:#475569;
  font-size:11.5px; font-weight:550; white-space:nowrap; }}
.pill {{ display:inline-block; padding:2px 9px; border-radius:999px; border:1px solid; font-size:11.5px;
  font-weight:600; white-space:nowrap; }}
.dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:7px; vertical-align:middle; }}
.dot-l {{ font-size:12.5px; color:var(--ink-2); }}
.conf {{ font-size:11.5px; font-weight:600; }}
.conf-high {{ color:#15803d; }} .conf-medium {{ color:#b45309; }} .conf-low {{ color:#b91c1c; }}
.ext {{ font-size:12.5px; }}
.bar {{ display:flex; align-items:center; gap:9px; }}
.bar .track {{ width:92px; height:6px; border-radius:999px; background:#eef0f4; overflow:hidden; }}
.bar .fill {{ height:100%; background:#22c55e; border-radius:999px; }}
.bar span {{ font-size:12px; color:var(--ink-2); font-variant-numeric:tabular-nums; }}
.chart {{ padding:6px 10px 12px; }}
.note {{ padding:16px 18px; font-size:13px; color:var(--ink-2); }}
.note code {{ background:#f1f5f9; padding:1px 5px; border-radius:4px; font-size:12px; }}
.empty {{ padding:22px 18px; color:var(--ink-3); font-size:13px; }}
footer {{ color:var(--ink-3); font-size:12px; padding:6px 0 0; }}
footer code {{ background:#eef0f4; padding:1px 5px; border-radius:4px; }}
@media (max-width:720px) {{ .wrap {{ padding:0 16px; }} td.title {{ max-width:170px; }} }}
</style></head>
<body>
<header><div class="wrap"><div class="hd">
  <div>
    <h1>Autonomous backlog swarm · <a href="https://github.com/{repo}">{repo}</a></h1>
    <div class="sub">Scanners file the work, Devin sessions do it, policy decides what merges.
    Every figure is derived from the Devin and GitHub APIs — the source data is published beside
    this page as <a href="state.json">state.json</a> and <a href="metrics.json">metrics.json</a>.</div>
  </div>
  <div class="live"><span class="dot" style="background:#16a34a"></span>Updated {generated}</div>
</div></div></header>

<main><div class="wrap">
  <div class="cards">{card_html}</div>

  <section>
    <div class="sh"><div><h2>Open work over time</h2>
      <p>Tasks the swarm is carrying, from the ledger's own timestamps.</p></div></div>
    <div class="chart">{_burndown_svg(ledger)}</div>
  </section>

  <section>
    <div class="sh">
      <div><h2>Tasks</h2><p>{c["tasks"]} tracked · click a column to sort</p></div>
      <div class="tools">
        <input type="search" id="q" placeholder="Filter tasks…" aria-label="Filter tasks">
        <button class="chip" data-filter="" aria-pressed="true">all<span class="ct">{c["tasks"]}</span></button>
        {chips}
      </div>
    </div>
    <table id="tasks"><thead><tr>
      <th class="sortable num" data-type="num">Issue</th>
      <th class="sortable">Title</th>
      <th class="sortable">Class</th>
      <th class="sortable">State</th>
      <th class="sortable num" data-type="num">PR</th>
      <th class="sortable">CI</th>
      <th class="sortable">Review</th>
      <th class="sortable">Confidence</th>
      <th class="sortable num">Size</th>
      <th class="sortable num" data-type="num">Msgs</th>
      <th>Session</th>
    </tr></thead>
    <tbody>{"".join(rows) or '<tr><td colspan="11" class="empty">No tasks yet.</td></tr>'}</tbody></table>
    <div class="empty" id="none" hidden>Nothing matches that filter.</div>
  </section>

  <section>
    <div class="sh"><div><h2>By issue class</h2>
      <p>Which kinds of work the swarm actually lands — and which it hands back.</p></div></div>
    <table><thead><tr><th>Class</th><th class="num">Tasks</th><th class="num">PRs</th>
      <th class="num">Merged</th><th>Merge rate</th></tr></thead>
    <tbody>{class_rows or '<tr><td colspan="5" class="empty">—</td></tr>'}</tbody></table>
  </section>

  <section>
    <div class="sh"><div><h2>Failure taxonomy</h2>
      <p>Separates "the agent is bad" from "our configuration is bad" — usually the latter.</p></div></div>
    <table><thead><tr><th>Category</th><th class="num">Tasks</th></tr></thead><tbody>{failure_rows}</tbody></table>
  </section>

  <section>
    <div class="sh"><div><h2>How this is measured</h2></div></div>
    <div class="note">{_cost_note(m)}</div>
  </section>

  <footer>
    Reconciler run <code>{_esc(str(ctx.get("run_id", "local")))}</code> ·
    ledger schema v{ledger.schema_version} ·
    no server, no database: this page is a file on the <code>gh-pages</code> branch.
  </footer>
</div></main>

<script>
(function () {{
  var table = document.getElementById('tasks');
  if (!table) return;
  var body = table.tBodies[0];
  var rows = Array.prototype.slice.call(body.rows);
  var q = document.getElementById('q');
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  var none = document.getElementById('none');
  var state = '';

  function apply() {{
    var needle = (q.value || '').toLowerCase().trim();
    var shown = 0;
    rows.forEach(function (r) {{
      var ok = (!state || r.dataset.state === state) &&
               (!needle || (r.dataset.q || '').indexOf(needle) !== -1);
      r.hidden = !ok;
      if (ok) shown++;
    }});
    none.hidden = shown !== 0;
  }}

  q.addEventListener('input', apply);
  chips.forEach(function (chip) {{
    chip.addEventListener('click', function () {{
      state = chip.dataset.filter;
      chips.forEach(function (o) {{ o.setAttribute('aria-pressed', String(o === chip)); }});
      apply();
    }});
  }});

  // Sorting: numeric columns on their digits, everything else on visible text.
  Array.prototype.slice.call(table.tHead.rows[0].cells).forEach(function (th, i) {{
    if (th.className.indexOf('sortable') === -1) return;
    th.addEventListener('click', function () {{
      var dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
      Array.prototype.slice.call(table.tHead.rows[0].cells)
        .forEach(function (o) {{ delete o.dataset.dir; }});
      th.dataset.dir = dir;
      var numeric = th.dataset.type === 'num';
      var sign = dir === 'asc' ? 1 : -1;
      rows.sort(function (a, b) {{
        var x = a.cells[i].innerText.trim(), y = b.cells[i].innerText.trim();
        if (numeric) {{
          var nx = parseFloat(x.replace(/[^0-9.\\-]/g, '')), ny = parseFloat(y.replace(/[^0-9.\\-]/g, ''));
          if (isNaN(nx)) return 1;            // blanks sort last in both directions
          if (isNaN(ny)) return -1;
          return (nx - ny) * sign;
        }}
        if (x === '—') return 1;
        if (y === '—') return -1;
        return x.localeCompare(y) * sign;
      }});
      rows.forEach(function (r) {{ body.appendChild(r); }});
    }});
  }});
}})();
</script>
</body></html>
"""


def _bar(rate: float) -> str:
    pct = max(0.0, min(1.0, rate)) * 100
    return (
        f'<span class="bar"><span class="track"><span class="fill" style="width:{pct:.0f}%"></span></span>'
        f"<span>{pct:.0f}%</span></span>"
    )


def _cost_note(m: dict) -> str:
    """Say which unit the account is billed in rather than inventing one."""
    if m["acus_metered"]:
        return (
            f"{m['acus_total']} ACUs across every session, {m['acus_per_merged_pr']} per merged PR — "
            "total spend divided by merges, so failed attempts are charged to the changes that landed."
        )
    sizes = ", ".join(f"{n}×{k}" for k, n in m["effort"]["session_sizes"].items()) or "—"
    return (
        "<strong>There is no cost metric on this page because this account does not report one.</strong> "
        "ACUs are the Enterprise billing unit; here <code>acus_consumed</code> is <code>0.0</code> on every "
        "session and <code>GET /consumption/daily</code> reports <code>total_acus: 0.0</code>, because the plan "
        "bills included quota plus on-demand credits, which the API does not expose per session. Rather than "
        "print a fabricated unit cost, effort is reported in the units the API does return: Devin's own session "
        f"size classification ({sizes}), {m['effort']['devin_messages_total']} Devin messages, and wall-clock "
        "session time. The daily budget cap still binds — each dispatch reserves its class's per-session ACU "
        "limit up front instead of trusting an unmetered zero."
    )


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _burndown_svg(ledger: Ledger, width: int = 1100, height: int = 150) -> str:
    """Cumulative open-task count over time, from ledger timestamps only."""
    events: list[tuple] = []
    for t in ledger.tasks.values():
        events.append((t.created_at, 1))
        if t.state == MERGED and t.terminal_at:
            events.append((t.terminal_at, -1))
    if len(events) < 2:
        return '<p class="muted" style="padding:0 8px 6px">Not enough history yet to draw a chart.</p>'
    events.sort()
    t0, t1 = events[0][0], max(e[0] for e in events)
    span = max(1, t1 - t0)
    open_count = 0
    points: list[tuple[float, int]] = []
    peak = 1
    for ts, delta in events:
        open_count += delta
        peak = max(peak, open_count)
        points.append((ts, open_count))

    def xy(ts: float, val: int) -> tuple[float, float]:
        return (
            (ts - t0) / span * (width - 24) + 12,
            height - 26 - (val / peak) * (height - 52),
        )

    coords = [xy(ts, val) for ts, val in points]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    area = f"{line} L{coords[-1][0]:.1f},{height - 26:.1f} L{coords[0][0]:.1f},{height - 26:.1f} Z"
    gridlines = "".join(
        f'<line x1="12" x2="{width - 12}" y1="{height - 26 - f * (height - 52):.1f}" '
        f'y2="{height - 26 - f * (height - 52):.1f}" stroke="#eef0f4" stroke-width="1"/>'
        for f in (0, 0.5, 1)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
        f'preserveAspectRatio="none" aria-label="open swarm tasks over time">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="#2563eb" stop-opacity=".16"/>'
        f'<stop offset="100%" stop-color="#2563eb" stop-opacity="0"/></linearGradient></defs>'
        f"{gridlines}"
        f'<path d="{area}" fill="url(#g)"/>'
        f'<path d="{line}" fill="none" stroke="#2563eb" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<text x="12" y="16" fill="#8a94a6" font-size="11" font-family="system-ui">'
        f"open tasks · peak {peak}</text>"
        f'<text x="{width - 12}" y="{height - 8}" fill="#8a94a6" font-size="11" '
        f'font-family="system-ui" text-anchor="end">now</text>'
        f'<text x="12" y="{height - 8}" fill="#8a94a6" font-size="11" font-family="system-ui">'
        f"first task</text>"
        f"</svg>"
    )


def render_metrics_json(ledger: Ledger) -> str:
    return json.dumps(compute(ledger), indent=2, sort_keys=True) + "\n"

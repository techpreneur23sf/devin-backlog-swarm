"""Issue schema: the machine-readable contract between backlog and swarm.

The quality of the issue is the ceiling on the quality of the PR, so an issue
is not free-form prose. It carries a metadata block that the dispatcher reads
directly:

    <!-- swarm-meta: {"class": "deprecation", "touch_scope": ["superset/utils/**"],
                      "verify": ["pytest tests/unit_tests/utils"], "fingerprint": "..."} -->

`class` selects the playbook and the trust tier, `touch_scope` feeds the
conflict-aware scheduler, `verify` becomes the session's definition of done,
and `fingerprint` is the dedupe key for scanner-generated issues.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

META_RE = re.compile(r"<!--\s*swarm-meta:\s*(\{.*?\})\s*-->", re.DOTALL)
FINDING_RE = re.compile(r"<!--\s*swarm-finding:\s*([A-Za-z0-9_\-]+)\s*-->")


@dataclass
class IssueSpec:
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    issue_class: str = "unclassified"
    touch_scope: list[str] = field(default_factory=lambda: ["**"])
    verify: list[str] = field(default_factory=list)
    fingerprint: str | None = None
    tier: str | None = None

    @classmethod
    def from_issue(cls, issue: dict[str, Any]) -> IssueSpec:
        body = issue.get("body") or ""
        labels = [lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in issue.get("labels", [])]
        meta: dict[str, Any] = {}
        m = META_RE.search(body)
        if m:
            try:
                meta = json.loads(m.group(1))
            except json.JSONDecodeError:
                meta = {}
        klass = meta.get("class") or next(
            (lbl.split(":", 1)[1] for lbl in labels if lbl.startswith("class:")), "unclassified"
        )
        return cls(
            number=issue["number"],
            title=issue.get("title", ""),
            body=body,
            labels=labels,
            issue_class=klass,
            touch_scope=meta.get("touch_scope") or ["**"],
            verify=meta.get("verify") or [],
            fingerprint=meta.get("fingerprint"),
            tier=meta.get("tier"),
        )


def render_meta(
    issue_class: str,
    touch_scope: list[str],
    verify: list[str],
    fingerprint: str | None = None,
    tier: str | None = None,
) -> str:
    payload: dict[str, Any] = {"class": issue_class, "touch_scope": touch_scope, "verify": verify}
    if fingerprint:
        payload["fingerprint"] = fingerprint
    if tier:
        payload["tier"] = tier
    return f"<!-- swarm-meta: {json.dumps(payload, sort_keys=True)} -->"


def render_issue_body(
    problem: str,
    affected_paths: list[str],
    acceptance: list[str],
    verify: list[str],
    issue_class: str,
    touch_scope: list[str],
    fingerprint: str | None = None,
    tier: str | None = None,
    provenance: str | None = None,
) -> str:
    parts = [problem.strip(), ""]
    parts.append("## Affected paths\n")
    parts += [f"- `{p}`" for p in affected_paths]
    parts.append("\n## Acceptance criteria\n")
    parts += [f"- {a}" for a in acceptance]
    parts.append("\n## Verification commands\n")
    parts.append("```bash")
    parts += verify
    parts.append("```")
    if provenance:
        parts.append(f"\n_{provenance}_")
    parts.append("")
    parts.append(render_meta(issue_class, touch_scope, verify, fingerprint, tier))
    if fingerprint:
        parts.append(f"<!-- swarm-finding: {fingerprint} -->")
    return "\n".join(parts)

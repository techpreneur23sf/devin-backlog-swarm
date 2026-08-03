"""Scanner adapters: findings -> deduplicated GitHub issues.

Devin's own Security Swarm is enterprise-gated and the native GitHub
Automations only work on private repositories, so this system does not ship a
scanner. It ships an *adapter*: `osv-scanner`, `pip-audit` and `semgrep` are
implemented here, and the interface is small enough that Snyk, Wiz, SonarQube
or Dependabot alerts drop into the same pipeline. Every enterprise already owns
a scanner; almost none of them own a remediation layer.

Dedupe is on a finding fingerprint (ecosystem + package + advisory + file), not
on the issue title, because titles change and crons overlap.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .gh import GitHubClient
from .issues import FINDING_RE, render_issue_body

SCANNER_PROVENANCE = "Filed automatically by `swarm scan` from a scanner finding."


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class Finding:
    tool: str
    ecosystem: str
    package: str
    advisory: str
    severity: str
    current_version: str
    fixed_version: str | None
    file: str
    summary: str
    issue_class: str = "dep-bump-patch"
    touch_scope: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)
    #: other advisory ids for the same package, folded into one issue
    aliases: list[str] = field(default_factory=list)
    #: every requirements file the package is pinned in
    files: list[str] = field(default_factory=list)

    #: every tool that reported this package, once findings are collapsed
    tools: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """Identity of the *finding*, deliberately independent of the scanner.

        Two scanners reporting the same vulnerable pin is one piece of work. When
        the tool was part of the key, adding `pip-audit` alongside `osv-scanner`
        refiled three packages that were already tracked.
        """
        return _sha(f"{self.ecosystem}|{self.package}|{self.current_version}")

    @property
    def legacy_fingerprints(self) -> list[str]:
        """Pre-existing issues were filed with the tool in the key."""
        return [
            _sha(f"{tool}|{self.ecosystem}|{self.package}|{self.current_version}")
            for tool in ("osv-scanner", "pip-audit", "semgrep")
        ]

    @property
    def title(self) -> str:
        if self.tool == "semgrep":
            return f"[{self.severity.lower()}] {self.advisory} in {self.file}"
        fix = f" → {self.fixed_version}" if self.fixed_version else ""
        extra = f" (+{len(self.aliases)} more)" if self.aliases else ""
        return f"[{self.severity.lower()}] {self.package} {self.current_version}{fix}: {self.advisory}{extra}"


def collapse(findings: Sequence[Finding]) -> list[Finding]:
    """One issue per package, not per advisory and not per file.

    Scanners emit the same vulnerability twice (GHSA and PYSEC ids are aliases),
    a package with six advisories still needs exactly one bump, and a package
    pinned in both `base.txt` and `development.txt` needs one PR touching both.
    Filing them separately would guarantee sessions racing on the same file.
    """
    grouped: dict[str, Finding] = {}
    for f in findings:
        key = f.fingerprint if f.tool != "semgrep" else f"semgrep|{f.advisory}|{f.file}"
        head = grouped.get(key)
        if head is None:
            f.files = [f.file]
            f.tools = [f.tool]
            grouped[key] = f
            continue
        if f.tool not in head.tools:
            head.tools.append(f.tool)
        if f.file not in head.files:
            head.files.append(f.file)
        if f.advisory not in head.aliases and f.advisory != head.advisory:
            head.aliases.append(f.advisory)
        # keep the highest fixed version we were offered
        if f.fixed_version and (not head.fixed_version or _vtuple(f.fixed_version) > _vtuple(head.fixed_version)):
            head.fixed_version = f.fixed_version
        if len(head.summary) < len(f.summary):
            head.summary = f.summary
    out = list(grouped.values())
    for f in out:
        f.issue_class = _classify(f)
    return out


def _classify(f: Finding) -> str:
    """A major-version bump is not the same class of work as a patch bump.

    Renovate can do the second. The first breaks call sites, which is precisely
    the boundary this system exists to cross, so it gets a different class, a
    different playbook and a different trust tier.
    """
    if f.issue_class != "dep-bump-patch" or not f.fixed_version:
        return f.issue_class
    cur, fix = _vtuple(f.current_version), _vtuple(f.fixed_version)
    if cur and fix and fix[0] > cur[0]:
        return "dep-bump-major"
    return "dep-bump-patch"


def _vtuple(version: str):
    parts = []
    for chunk in re.split(r"[.\-+]", version or ""):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


# -- adapters -----------------------------------------------------------------
@dataclass
class ToolRun:
    """What one scanner did, so a scan that found nothing can be told apart
    from a scanner that never ran.

    A silent skip is the worst outcome intake can have: an empty backlog looks
    identical to a clean repository, and nobody investigates good news.
    """

    tool: str
    status: str  # ok | missing | error
    findings: int = 0
    detail: str = ""


def _run(cmd: Sequence[str], cwd: str, report: list[ToolRun] | None = None) -> str | None:
    if not shutil.which(cmd[0]):
        if report is not None:
            report.append(ToolRun(cmd[0], "missing", detail="not installed on this runner"))
        return None
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if not proc.stdout:
        if report is not None:
            tail = (proc.stderr or "").strip().splitlines()
            report.append(
                ToolRun(cmd[0], "error", detail=f"exit {proc.returncode}: {tail[-1] if tail else 'no output'}"[:200])
            )
        return None
    return proc.stdout


def parse_osv(output: str, default_file: str = "requirements/base.txt") -> list[Finding]:
    data = json.loads(output)
    findings: list[Finding] = []
    for result in data.get("results", []):
        source = (result.get("source") or {}).get("path", default_file)
        for pkg in result.get("packages", []):
            info = pkg.get("package", {})
            for vuln in pkg.get("vulnerabilities", []):
                fixed = _osv_fixed_version(vuln, info.get("name", ""))
                findings.append(
                    Finding(
                        tool="osv-scanner",
                        ecosystem=info.get("ecosystem", "PyPI"),
                        package=info.get("name", "?"),
                        advisory=vuln.get("id", "?"),
                        severity=_osv_severity(vuln),
                        current_version=info.get("version", "?"),
                        fixed_version=fixed,
                        file=os.path.basename(source),
                        summary=(vuln.get("summary") or vuln.get("details") or "")[:400],
                    )
                )
    return findings


def _osv_severity(vuln: dict[str, Any]) -> str:
    db = (vuln.get("database_specific") or {}).get("severity")
    if db:
        return str(db)
    for s in vuln.get("severity", []) or []:
        if s.get("type") == "CVSS_V3":
            return "high"
    return "moderate"


def _osv_fixed_version(vuln: dict[str, Any], package: str) -> str | None:
    for affected in vuln.get("affected", []) or []:
        for rng in affected.get("ranges", []) or []:
            for event in rng.get("events", []) or []:
                if event.get("fixed"):
                    return event["fixed"]
    return None


def parse_pip_audit(output: str, file: str = "requirements/base.txt") -> list[Finding]:
    data = json.loads(output)
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    findings: list[Finding] = []
    for dep in deps or []:
        for vuln in dep.get("vulns", []) or []:
            fixes = vuln.get("fix_versions") or []
            findings.append(
                Finding(
                    tool="pip-audit",
                    ecosystem="PyPI",
                    package=dep.get("name", "?"),
                    advisory=vuln.get("id", "?"),
                    severity="moderate",
                    current_version=dep.get("version", "?"),
                    fixed_version=fixes[0] if fixes else None,
                    file=file,
                    summary=(vuln.get("description") or "")[:400],
                )
            )
    return findings


#: semgrep impacts worth a human's attention. `p/python` on a codebase this size
#: reports plenty of LOW/INFO style hits; filing them buries the ones that matter.
SEMGREP_IMPACTS = ("medium", "high", "critical", "error")


def parse_semgrep(output: str, min_impact: bool = True) -> list[Finding]:
    data = json.loads(output)
    findings: list[Finding] = []
    for res in data.get("results", []) or []:
        extra = res.get("extra") or {}
        path = res.get("path", "")
        severity = str((extra.get("metadata") or {}).get("impact", extra.get("severity", "moderate"))).lower()
        if min_impact and severity not in SEMGREP_IMPACTS:
            continue
        findings.append(
            Finding(
                tool="semgrep",
                ecosystem="code",
                package=path,
                advisory=res.get("check_id", "?").split(".")[-1],
                severity=severity,
                current_version="",
                fixed_version=None,
                file=path,
                summary=(extra.get("message") or "")[:400],
                issue_class="security",
                touch_scope=[path],
            )
        )
    return findings


def run_scanners(
    repo_dir: str, tools: Sequence[str] = ("osv-scanner", "pip-audit", "semgrep")
) -> tuple[list[Finding], list[ToolRun]]:
    findings: list[Finding] = []
    report: list[ToolRun] = []
    if "osv-scanner" in tools:
        # osv-scanner v2 only recognises a requirements file by name, so each
        # of Superset's requirements/*.txt is passed explicitly.
        args = ["osv-scanner", "scan", "source", "--format", "json"]
        for req in ("requirements/base.txt", "requirements/development.txt"):
            if os.path.exists(os.path.join(repo_dir, req)):
                args += ["-L", f"requirements.txt:{req}"]
        out = _run(args, repo_dir, report)
        if out:
            parsed = parse_osv(out)
            findings += parsed
            report.append(ToolRun("osv-scanner", "ok", len(parsed)))
    if "pip-audit" in tools:
        for req in ("requirements/base.txt", "requirements/development.txt"):
            if os.path.exists(os.path.join(repo_dir, req)):
                out = _run(["pip-audit", "-r", req, "-f", "json", "--progress-spinner", "off"], repo_dir, report)
                if out:
                    parsed = parse_pip_audit(out, req)
                    findings += parsed
                    report.append(ToolRun("pip-audit", "ok", len(parsed), detail=req))
    if "semgrep" in tools:
        out = _run(["semgrep", "--config", "p/python", "--json", "--quiet", "superset"], repo_dir, report)
        if out:
            parsed = parse_semgrep(out)
            findings += parsed
            report.append(ToolRun("semgrep", "ok", len(parsed)))
    return collapse(findings), report


# -- finding -> issue ---------------------------------------------------------
def existing_fingerprints(gh: GitHubClient) -> dict[str, int]:
    out: dict[str, int] = {}
    for issue in gh.list_issues(state="all"):
        for fp in FINDING_RE.findall(issue.get("body") or ""):
            out[fp] = issue["number"]
    return out


def finding_to_issue_body(f: Finding) -> str:
    if f.tool == "semgrep":
        problem = (
            f"`semgrep` flagged `{f.advisory}` in `{f.file}`.\n\n> {f.summary}\n\n"
            "Fix the underlying issue at this boundary and add a regression test that fails "
            "without the fix."
        )
        verify = [f"python -m pytest -q tests/unit_tests -k {os.path.basename(f.file).replace('.py', '')}"]
        touch = f.touch_scope or [f.file]
    else:
        target = f" to `{f.fixed_version}` or newer" if f.fixed_version else ""
        advisories = ", ".join(f"`{a}`" for a in [f.advisory] + f.aliases)
        files = f.files or [f.file]
        where = ", ".join(f"`requirements/{x}`" for x in files)
        reporters = ", ".join(f"`{t}`" for t in (f.tools or [f.tool]))
        problem = (
            f"{reporters} reports {advisories} affecting `{f.package}=={f.current_version}` "
            f"pinned in {where}.\n\n> {f.summary}\n\n"
            f"Bump `{f.package}`{target} in every file above. Superset compiles "
            f"`requirements/*.txt` from the matching `requirements/*.in` with `pip-compile`, so "
            f"if the package is named in the `.in` file, update it there too. Keep the diff to "
            f"the pin — no unrelated re-compilation of the whole file."
        )
        verify = [f"grep -rn '^{f.package}==' requirements/"]
        touch = [f"requirements/{x}" for x in files] + [f"requirements/{x.replace('.txt', '.in')}" for x in files]
    return render_issue_body(
        problem=problem,
        affected_paths=[f.file] if f.tool == "semgrep" else [f"requirements/{x}" for x in (f.files or [f.file])],
        acceptance=[
            f"`{f.package}` is pinned outside the vulnerable range for {f.advisory}"
            + (f" (and {len(f.aliases)} aliased advisories)" if f.aliases else ""),
            "No unrelated dependency or code changes in the diff",
            "The verification commands below run clean",
        ],
        verify=verify,
        issue_class=f.issue_class,
        touch_scope=touch,
        fingerprint=f.fingerprint,
        tier="tier-1",
        provenance=SCANNER_PROVENANCE,
    )


def file_issues(
    gh: GitHubClient,
    findings: Iterable[Finding],
    auto_label: bool = False,
    limit: int = 25,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Create one issue per new finding. Idempotent on the fingerprint."""
    known = existing_fingerprints(gh) if not dry_run else {}
    created: list[dict[str, Any]] = []
    for f in list(findings)[:limit]:
        if any(fp in known for fp in [f.fingerprint, *f.legacy_fingerprints]):
            continue
        labels = [f"class:{f.issue_class}", "tier:1", "swarm:queued", "scanner"]
        if auto_label:
            labels.append("devin:auto")
        if dry_run:
            created.append({"title": f.title, "labels": labels, "fingerprint": f.fingerprint})
            continue
        issue = gh.create_issue(f.title, finding_to_issue_body(f), labels)
        created.append(issue)
        known[f.fingerprint] = issue["number"]
    return created

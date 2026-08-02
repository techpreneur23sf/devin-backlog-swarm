"""Policy engine: trust tiers, budgets, kill switch.

The policy file is the answer to the question a VP of Engineering asks about
thirty seconds into the demo: "you let an agent merge to main?" No — a class of
change with green CI, a clean Devin Review, high self-reported confidence and a
bounded diff merges automatically; anything touching auth or security is never
auto-merged regardless of how clean it looks.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_PATH = os.environ.get("SWARM_POLICY", "policy.yaml")


@dataclass
class Budget:
    daily_acu_cap: float = 120
    per_session_acu_cap: int = 12
    max_concurrent_sessions: int = 6
    max_attempts_per_issue: int = 2
    #: reconcile ticks a session may sit in waiting_for_user before we sweep it
    waiting_for_user_ticks: int = 3


@dataclass
class Tier:
    name: str
    classes: list[str] = field(default_factory=list)
    require: dict[str, Any] = field(default_factory=dict)
    matches: dict[str, Any] = field(default_factory=dict)
    auto_merge: bool = True


@dataclass
class Policy:
    kill_switch: bool = False
    budget: Budget = field(default_factory=Budget)
    tiers: list[Tier] = field(default_factory=list)
    class_priority: list[str] = field(default_factory=list)
    playbooks: dict[str, str] = field(default_factory=dict)
    acu_limit_by_class: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- loading --------------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> Policy:
        p = Path(path or DEFAULT_POLICY_PATH)
        data = yaml.safe_load(p.read_text()) or {} if p.exists() else {}
        budget = Budget(**{k: v for k, v in (data.get("budget") or {}).items() if k in Budget.__annotations__})
        # Env overrides exist so an operator can throttle or stop the swarm from
        # the Actions UI without a commit — the kill switch has to be reachable
        # faster than a pull request.
        if os.environ.get("SWARM_MAX_SESSIONS"):
            budget.max_concurrent_sessions = int(os.environ["SWARM_MAX_SESSIONS"])
        if os.environ.get("SWARM_DAILY_ACU_CAP"):
            budget.daily_acu_cap = float(os.environ["SWARM_DAILY_ACU_CAP"])
        kill = bool(data.get("kill_switch", False))
        if os.environ.get("SWARM_KILL_SWITCH", "").lower() in ("1", "true", "yes"):
            kill = True
        tiers = []
        for name, spec in (data.get("tiers") or {}).items():
            spec = spec or {}
            tiers.append(
                Tier(
                    name=name,
                    classes=spec.get("classes") or [],
                    require=spec.get("require") or {},
                    matches=spec.get("matches") or {},
                    auto_merge=not name.startswith("always_human"),
                )
            )
        return cls(
            kill_switch=kill,
            budget=budget,
            tiers=tiers,
            class_priority=data.get("class_priority") or [],
            playbooks=data.get("playbooks") or {},
            acu_limit_by_class=data.get("acu_limit_by_class") or {},
            raw=data,
        )

    # -- lookups --------------------------------------------------------------
    def tier_for(self, issue_class: str) -> Tier | None:
        human = [t for t in self.tiers if not t.auto_merge]
        for t in human:
            if issue_class in t.classes:
                return t
        for t in self.tiers:
            if issue_class in t.classes:
                return t
        return None

    def priority(self, issue_class: str) -> int:
        try:
            return self.class_priority.index(issue_class)
        except ValueError:
            return len(self.class_priority) + 1

    def acu_limit(self, issue_class: str) -> int:
        return int(self.acu_limit_by_class.get(issue_class, self.budget.per_session_acu_cap))

    def playbook_for(self, issue_class: str) -> str | None:
        return self.playbooks.get(issue_class) or self.playbooks.get("default")


@dataclass
class MergeDecision:
    allowed: bool
    reason: str
    tier: str | None = None


def _paths_match(paths: Sequence[str], globs: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(p, g) for p in paths for g in globs)


def evaluate_merge(
    policy: Policy,
    issue_class: str,
    ci_status: str | None,
    review_status: str | None,
    structured_output: dict[str, Any] | None,
    files_changed: Sequence[str],
) -> MergeDecision:
    """Decide whether a PR may be merged without a human.

    Every input is an observed fact: CI from the checks API, review from the
    Devin Review verdict, confidence/blockers from the session's structured
    output, files from the PR's file list.
    """
    if policy.kill_switch:
        return MergeDecision(False, "kill switch engaged")

    tier = policy.tier_for(issue_class)
    if tier is None:
        return MergeDecision(False, f"class '{issue_class}' matches no policy tier; human review", None)

    so = structured_output or {}
    blockers = so.get("blockers") or []

    if not tier.auto_merge:
        return MergeDecision(False, f"class '{issue_class}' is in the always-human tier", tier.name)

    human_matches = [t for t in policy.tiers if not t.auto_merge]
    for ht in human_matches:
        globs = (ht.matches or {}).get("paths") or []
        if globs and _paths_match(files_changed, globs):
            return MergeDecision(False, f"diff touches protected paths ({ht.name})", ht.name)
        if (ht.matches or {}).get("blockers_nonempty") and blockers:
            return MergeDecision(False, f"session reported blockers ({ht.name})", ht.name)

    req = tier.require or {}
    if req.get("ci") == "green" and ci_status != "green":
        return MergeDecision(False, f"CI is {ci_status or 'unknown'}", tier.name)
    if req.get("review") == "clean" and review_status != "clean":
        return MergeDecision(False, f"review is {review_status or 'pending'}", tier.name)

    want_conf = req.get("confidence")
    if want_conf:
        want = [want_conf] if isinstance(want_conf, str) else list(want_conf)
        if so.get("confidence") not in want:
            return MergeDecision(False, f"confidence {so.get('confidence')!r} not in {want}", tier.name)

    if so.get("outcome") not in (None, "fixed"):
        return MergeDecision(False, f"outcome is {so.get('outcome')!r}", tier.name)
    if so.get("verification_passed") is False:
        return MergeDecision(False, "session reported verification failed", tier.name)
    if blockers:
        return MergeDecision(False, "session reported blockers", tier.name)

    max_files = req.get("max_files_changed")
    if max_files is not None and len(files_changed) > int(max_files):
        return MergeDecision(False, f"{len(files_changed)} files changed > {max_files}", tier.name)

    return MergeDecision(True, f"satisfies tier '{tier.name}'", tier.name)

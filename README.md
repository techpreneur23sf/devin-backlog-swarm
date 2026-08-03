# devin-backlog-swarm

An autonomous remediation layer for engineering backlog debt: it finds the work,
dispatches Devin sessions at it, watches what actually happens, and merges only
what has earned it.

Backlogs rot because remediation is nobody's job. This makes it a system's job —
a scanner files the work, a scheduler rations it, a reconciler advances each task
on observed API responses, and a merge policy decides what lands without a human.

It runs entirely on GitHub Actions. No service, no database, nothing to keep up.

## Try it without a Devin API key

```bash
git clone https://github.com/techpreneur23sf/devin-backlog-swarm
cd devin-backlog-swarm
pip install -e .
swarm replay --replay fixtures/run-2026-08-03/
```

That walks a **recorded run of the real thing** — actual Devin and GitHub API
responses captured from the live swarm operating on
[techpreneur23sf/apache-superset](https://github.com/techpreneur23sf/apache-superset).
No credentials, no network calls. An unrecorded request raises `ReplayMiss`
rather than falling back to anything, because fixtures are recordings and replay
never invents data.

## What runs where

```
issue labelled devin:auto ──► swarm dispatch ──► Devin session (tagged, ACU-capped)
nightly cron ─────────────► swarm nightly  ──► conflict-aware fan-out
every 15 min ─────────────► swarm reconcile ─► observe → transition → publish
PR opened ────────────────► swarm verify   ──► the issue's own verification commands
weekly ───────────────────► swarm scan     ──► OSS scanners → deduplicated issues
```

The workflows in the target repository are thin: they pass credentials to this
container and do nothing else. All logic is here, so the same image runs on a
laptop, in Actions, in GitLab CI or in Jenkins.

## The state machine

```
queued → dispatched → running → { pr_open | needs_human | failed }
pr_open → review_pending → review_clean → merged
```

A task only moves on an API response the reconciler read this tick. Nothing is
inferred from elapsed time or from a session's own claim of success.

- `state.json` on the orphan `swarm-state` branch is the ledger (atomic,
  versioned, diffable — every tick is a commit).
- `swarm:*` labels mirror it for humans and are derived, never authoritative.
- Devin session tags let `swarm state rebuild` reconstruct the ledger from the
  Devin and GitHub APIs alone if the branch is lost.

## An issue is a contract

```markdown
<!-- swarm-meta: {"class": "deprecation",
                  "touch_scope": ["superset/tasks/cron_util.py"],
                  "verify": ["pytest tests/unit_tests/tasks/test_cron_util.py -q"]} -->
```

`class` picks the policy tier and the playbook, `touch_scope` is what the
scheduler serialises on *and* what the diff is checked against, and `verify` is
the PR's CI: `swarm verify` runs exactly those commands. An issue that declares
nothing verifiable cannot produce a green PR.

## What gets merged automatically

`evaluate_merge` requires all of: green CI, a clean Devin Review, high
confidence, a `fixed` outcome, `verification_passed`, no blockers, a bounded
diff, no protected paths, and an auto-merge tier. Security and authorization
classes are never auto-merged. `Devin Review`'s own commit status is explicitly
excluded from the CI computation — a reviewer agreeing with the author is not a
test run.

Budgets and the stop button are environment-overridable, because a kill switch
that needs a commit and a review is not a kill switch:

```bash
SWARM_KILL_SWITCH=true      # dispatch nothing, merge nothing
SWARM_MAX_SESSIONS=2        # concurrency
SWARM_DAILY_ACU_CAP=40      # spend
```

The daily cap is enforced against `max(reserved, observed)`, where each dispatch
reserves its class's per-session ACU limit. That is not belt-and-braces: Devin
meters ACUs on Enterprise plans, and reports `acus_consumed: 0.0` on plans billed
as quota plus on-demand credits — a cap compared against an unmetered zero would
never bind. For the same reason the dashboard reports "not metered" rather than a
cost per merged PR of 0.0, and falls back to the effort signals the API does
return per session (`session_size`, Devin message counts, wall-clock time).

## Commands

| Command | What it does |
|---|---|
| `swarm doctor` | verifies every API capability the system assumes |
| `swarm bootstrap` | labels, `swarm-state`, `gh-pages` |
| `swarm scan` | OSS scanners → deduplicated issues (fingerprinted, never refiled) — see [`docs/SCANS.md`](docs/SCANS.md) |
| `swarm seed` | files the hand-curated backlog from `backlog/*.yaml` |
| `swarm dispatch --issue N` | one session, non-blocking |
| `swarm nightly` | conflict-aware fan-out under concurrency and ACU caps |
| `swarm reconcile [--publish]` | the loop: observe, transition, merge, publish |
| `swarm verify --pr N` | run the linked issue's verification commands |
| `swarm state show\|metrics\|rebuild` | inspect or reconstruct the ledger |
| `swarm digest` | the morning digest issue |
| `swarm replay --replay DIR` | walk a recorded run offline |

## Setup against your own repository

1. `swarm bootstrap` (needs `GITHUB_TOKEN`, `DEVIN_API_KEY`, `DEVIN_ORG_ID`).
2. Copy `.github/workflows/swarm-*.yml` from the target repo into yours and set
   the three secrets.
3. Adjust `policy.yaml` — tiers, protected paths, budgets, playbooks.
4. Label an issue `devin:auto`.

## Reading the design

[`DECISIONS.md`](DECISIONS.md) has the verified Devin v3 capability table, the
honest caveats about it, what the live run broke, and why the rejected
alternatives were rejected. [`docs/SCANS.md`](docs/SCANS.md) covers intake: what
each scanner actually inspects, how 40 advisory records collapse into 8 issues,
and why the finding's *class* is what makes it dispatchable.

# Decisions

What was verified, what was chosen, and what was rejected. Every capability
claim below was checked against the live APIs during this build; where a
capability turned out not to exist, the design changed rather than the claim.

## Devin API capability table

Checked against `api.devin.ai` on 2026-08-01 with a service-user key
(`devin-service-user-superset`, org `org-252e5f3a…`). `swarm doctor` re-checks
the load-bearing ones on demand.

| Capability | Endpoint | Result | What the design does with it |
|---|---|---|---|
| Identity / org discovery | `GET /v3/self` | works | resolves the org id so nothing is hard-coded |
| Create a session | `POST /v3/organizations/{org}/sessions` | works | dispatch; accepts `tags`, `repos`, `max_acu_limit`, `structured_output_schema` |
| Read a session | `GET …/sessions/{id}` | works | the reconciler's primary observation: `status`, `status_detail`, `acus_consumed`, `pull_requests`, `structured_output` |
| List sessions by tag | `GET …/sessions` | works | ledger rebuild without any local state |
| Send a message | `POST …/sessions/{id}/messages` | works | nudging a session that is waiting on a human |
| Terminate a session | `POST …/sessions/{id}` | works | the sweeper: stops sessions parked in `waiting_for_user` |
| Playbooks | `GET …/playbooks` | works | per-class playbook binding in `policy.yaml` |
| Request a review | `POST …/pr-reviews` | works | review gate |
| Read a review verdict | `GET …/pr-reviews?pr_url=…` | works | review gate |
| Daily consumption | `GET …/consumption/daily` | works, **reports 0.0** | budget enforcement (see below) |
| Enterprise code scanning | `GET /v3/enterprise/code-scans/findings` | **403** | not available to this org — scanner intake uses OSS adapters instead |

Two honest caveats:

- **ACU accounting reads zero.** Both `consumption/daily` and every session's
  `acus_consumed` returned `0.0` for this org while real sessions ran and
  produced PRs. The budget code is live and enforced against whatever the API
  reports; it simply has nothing to enforce against here. The dashboard shows
  the API's number rather than an estimate, and no ACU figure in this project
  is modelled, extrapolated, or filled in.
- **`status_detail: waiting_for_user`** is the signal that matters, not
  `status`. A session that has finished its work and is waiting for a human
  still reports `status: running`; treating `running` as "busy" would hold a
  concurrency slot forever.

## Enterprise scanner → OSS adapters

The enterprise scan endpoint is 403 for this org, so intake is built around the
*finding*, not the tool: `scan.py` normalises `osv-scanner` and `pip-audit`
output into one `Finding` shape, and anything that can produce that shape
(Snyk, Dependabot alerts, an internal scanner) plugs in without touching the
issue-filing or dispatch code.

Deduplication is the whole game here. The first real scan of the Superset fork
produced 40 advisory records covering 11 package/file pairs — the same package
pinned in two requirements files, and several advisories aliasing the same
vulnerability. Keying issues on the advisory id would have filed 40 issues and
re-filed them on the next run. The fingerprint is therefore
`sha256(tool|ecosystem|package|current_version)`: advisory ids and file paths
are folded into the issue body as aliases and affected files. That collapsed
the same scan to **8 real tasks**.

## GitHub as the entire runtime

No service, no database, no inbound network. The reasons are operational
rather than aesthetic: a customer can adopt this by copying five YAML files,
there is nothing to run at 3am that can be down, and every artifact is already
audited by the platform the work happens on.

| Concern | Where it lives | Why |
|---|---|---|
| Detailed task state | `state.json` on the orphan `swarm-state` branch | atomic, versioned, diffable; every reconcile tick is a commit with a message |
| Coarse state for humans | `swarm:*` labels on the issue | greppable in the GitHub UI by people who will never read `state.json` |
| Correlation / disaster recovery | Devin session tags | `swarm rebuild` reconstructs the ledger from the Devin and GitHub APIs alone |
| Public reporting | `index.html` + `metrics.json` + `state.json` on `gh-pages` | static, no server, and the JSON is the same object the reconciler wrote |
| Secrets | repository Actions secrets | never in the repo, redacted before anything is recorded to a fixture |

Labels are deliberately *derived*, never authoritative: a human editing a label
cannot corrupt the ledger, and label writes are best-effort because a failed
label update must not fail a reconcile tick.

## Why the reconciler polls

Devin session state is not delivered by webhook, so something has to poll
regardless; adding webhooks for the GitHub half would mean two code paths for
the same transition and a class of bugs where they disagree. A poll is
idempotent — a missed tick costs latency, not correctness — and the whole loop
is cheap enough to run every 15 minutes. `pull_request: closed` is wired as an
extra trigger purely to make merges show up promptly.

## The issue is the CI contract

The fork's 45 inherited Superset workflows are disabled (they are enormous, and
none of them gate the kind of change here). That leaves swarm PRs with no CI
signal — and the merge policy refuses to merge without one, which would have
been a permanent deadlock.

Rather than invent a generic pipeline, `swarm verify` runs the commands the
issue itself declared:

```
<!-- swarm-meta: {"class": "deprecation",
                  "touch_scope": ["superset/tasks/cron_util.py"],
                  "verify": ["pytest tests/unit_tests/tasks/test_cron_util.py -q"]} -->
```

CI runs exactly those commands, plus a check that the diff stayed inside
`touch_scope`. Consequences that are features: an issue that declares nothing
verifiable cannot produce a green PR, and a session that "fixed" the problem by
editing unrelated files fails CI even if the tests pass.

`Devin Review`'s own commit status is excluded from the CI computation
(`NON_CI_CONTEXTS`). It is a review signal; counting it as CI would let a PR
merge because the reviewer agreed with the author, with no test having run.

## Merge policy

Auto-merge requires *all* of: green CI, clean Devin Review, high confidence, a
`fixed` outcome, `verification_passed`, no reported blockers, a bounded number
of changed files, no protected paths in the diff, and a class in an auto-merge
tier. Security and authz classes are never auto-merged regardless of how clean
they look — the failure mode of a wrong security auto-merge is unbounded, so
that tier buys nothing and risks everything.

The kill switch, the concurrency cap and the daily ACU cap are all overridable
from environment variables (`SWARM_KILL_SWITCH`, `SWARM_MAX_SESSIONS`,
`SWARM_DAILY_ACU_CAP`), because a stop button that requires a commit and a
review is not a stop button.

## Conflict-aware scheduling

Two sessions editing the same files produce conflicting PRs and waste money, so
the scheduler is a greedy set-packing pass over declared touch scopes. Two
resources are rationed separately: a *running session* holds a concurrency slot
and its scope, while a task whose *PR is open but unmerged* holds only its
scope — a second session editing those files would conflict with a branch that
has not landed yet.

Overlap is computed on static glob prefixes and is deliberately conservative:
a false positive costs one tick of latency, a false negative costs a merge
conflict and a wasted session. This is visible in the real run — the second
dependency bump was skipped with "touch scope conflicts with a task already in
flight" because both edit `requirements/`.

## Replay

Replay is a property of the transport, not of a mock: every Devin and GitHub
call is keyed on `(method, url, body)` and recorded to a cassette, and replay
serves the recorded sequence per key, so a session polled five times appears to
change state exactly as it did live. The reconciler cannot tell the difference,
which is the point — the loop a reviewer watches offline is the same code that
ran against the real APIs.

An unrecorded request raises `ReplayMiss` rather than falling back to the
network or to a default. Fixtures are recordings of real runs, they are labelled
as such, and replay never invents data. Tokens are redacted before anything is
written to a cassette, and no `Authorization` header is recorded at all.

## Rejected alternatives

- **A long-running dispatcher service.** Would need hosting, monitoring and a
  database to survive restarts, to solve a problem a 15-minute cron already
  solves.
- **A database for the ledger.** `state.json` on a branch gives atomicity,
  history and a free audit trail; a database would give the same plus an
  operational burden.
- **Blocking dispatch jobs that wait for the session.** A session can take an
  hour; a job that waits burns Actions minutes to hold an integer. Dispatch
  creates the session and exits.
- **Merging on the Devin Review status alone.** Tempting once the fork's CI was
  disabled, and wrong — see above.
- **Estimating ACUs when the API reports zero.** Would make the dashboard look
  complete and be fiction.

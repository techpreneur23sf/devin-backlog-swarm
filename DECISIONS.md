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
| Session insights | `GET …/sessions/{id}/insights` | works | effort reporting on an account without ACU metering: `session_size`, message counts |
| Enterprise code scanning | `GET /v3/enterprise/code-scans/findings` | **403** | not available to this org — scanner intake uses OSS adapters instead |

Three honest caveats:

- **ACUs are the wrong unit for this account, not a broken one.** Both
  `consumption/daily` and every session's `acus_consumed` return `0.0` here
  while real sessions run and open PRs. ACUs are the *Enterprise* billing unit;
  self-serve plans consume included quota and then on-demand credits, and the
  API exposes neither per session ([docs](https://docs.devin.ai/admin/billing/usage)).
  So the swarm reports what the API does return per session
  (`GET …/sessions/{id}/insights`: Devin's own `session_size` class and message
  counts) plus wall-clock time, and the dashboard says "not metered" instead of
  printing a unit cost of 0.0 — shipped work advertised as free is the one
  rounding error a buyer would never forgive. Nothing is modelled or
  extrapolated.
- **An unmetered meter made the budget decorative.** The daily cap was compared
  against observed consumption, which on this plan is `0.0` forever, so the cap
  could never bind and the only real limit was `max_concurrent_sessions`. Every
  dispatch now *reserves* its class's per-session ACU limit for the day and the
  scheduler spends `max(reserved, observed)`: the cap bounds the blast radius on
  any plan, and a metered account still corrects it with real numbers.
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

## What the live run broke

Every item here was found by running the swarm against the fork for real, and
each one is a class of bug that only appears once observed reality disagrees
with the design.

- **The reviewer's verdict is not in the review API.** `GET /pr-reviews`
  reported `completed` with no verdict field; the findings are the PR review and
  inline comments the reviewer posts on GitHub. Reading the verdict from where
  it is published turned every task from "review pending" into a decision.
- **Overlapping writers reverted each other.** Dispatch jobs racing the
  reconciler on `state.json` lost updates, because the loser of the
  compare-and-swap re-applied its whole stale ledger. It now overlays only the
  tasks it changed onto freshly-read state.
- **A missing policy file was silently an empty policy.** In an Actions
  container the working directory is the workspace, not the image, so the
  relative `policy.yaml` did not exist — and an empty policy has no tiers, so
  every class "matched no tier" and every mergeable PR was parked for a human.
  The swarm looked conservative; it was misconfigured. A missing policy is now
  an error, with the image's own copy as the only fallback.
- **A parked task stopped watching its PR.** `needs_human` hands the PR to a
  person; when that person merged it, the ledger went on reporting it unmerged.
  Parked tasks keep observing PR state, and `merged_by` distinguishes a merge
  the policy performed from one a human performed — a human merge is a landed
  change, not autonomy, and the dashboard says so.
- **A suspended session held its files hostage.** The reconciler resolved `exit`
  and `error` sessions but not `suspended` ones, so a task whose session went
  idle sat in `dispatched` forever and starved every task sharing its touch
  scope. Two dependency issues were skipped as "conflicting" for hours because
  of it.
- **The budget could not be exceeded because it could not be spent.** See the
  unmetered-ACU note above: a cap enforced against a number the plan never
  populates is not a cap. Found by asking why every nightly run reported
  "0.0 / 120 ACUs" after dispatching six sessions.
- **A stale fixture replayed green.** The recorded run predated the reviewer
  change, so the documented stranger path emitted `ReplayMiss` for every review
  request — and still exited 0, because the loop deliberately swallows per-task
  errors. Replay now fails on any uncovered request, and CI runs it.

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
  complete and be fiction. The dashboard says "not metered", explains which unit
  the account is actually billed in, and reports the effort signals the API does
  return.
- **Dropping the daily budget because this plan does not meter it.** The cap is
  a safety property, not an accounting one: it exists so a bad scan cannot
  dispatch fifty sessions overnight. Reserving per dispatch keeps it real
  wherever the swarm is installed.

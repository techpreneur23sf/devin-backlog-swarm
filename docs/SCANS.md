# What the scan actually does

Intake is the front of the swarm: it turns machine findings into *issues written
for an agent*. This file is the reference for what each scanner looks at, what
it can and cannot see, and why the pipeline is shaped the way it is.

## The three intake paths

| Path | Trigger | Produces | Who writes the issue |
|---|---|---|---|
| `swarm scan` | weekly cron + manual dispatch | dependency vulnerability issues | the scanner, via `scan.py` |
| `swarm seed` | one-off, from `backlog/superset.yaml` | deprecation / code-quality / security issues | a human, once |
| A person | any time | anything | a person |

All three converge on the same artifact — a GitHub issue carrying a
`swarm-meta` block — and everything downstream only reads that. The swarm has no
opinion about where work comes from.

## The scanners

### `osv-scanner` (Google) — the one doing the work here

Scans **dependency manifests**, not code. It reads `requirements/base.txt` and
`requirements/development.txt`, resolves every pin to an exact version, and
queries [osv.dev](https://osv.dev) — the aggregate advisory database that
ingests GHSA (GitHub), PYSEC (PyPA), CVE/NVD and distro feeds — for advisories
whose affected range contains that version. For each hit it reports the
advisory id, severity, and the first version where the range closes, which is
the number the remediation issue needs.

What it catches: a pinned package with a published vulnerability, transitively
included or direct, in any ecosystem with a lockfile (PyPI here; npm, Go,
Maven, Cargo all supported by the same binary).

What it cannot catch: a vulnerability in your own code, a misconfiguration, or
an unpinned dependency.

Invocation is unusual enough to be worth noting: osv-scanner v2 identifies a
manifest by filename, and Superset's are `requirements/*.txt`, so each is passed
explicitly as `-L requirements.txt:requirements/base.txt`.

### `pip-audit` (PyPA) — the second opinion

Same class of check from a different source of truth: it resolves the
requirements file in a throwaway virtualenv and audits the *resolved* set against
PyPI's advisory feed. Because it resolves rather than parses, it sees transitive
packages a manifest does not name — and for the same reason it fails wherever
resolution fails. It contributes 8 raw findings on the CI runner
([run](https://github.com/techpreneur23sf/apache-superset/actions/runs/30853970636))
and fails outright on Python < 3.11, because Superset's own package metadata
requires 3.11+.

That asymmetry taught the pipeline two things.

**A scanner that crashes used to contribute silently zero findings** — which is
indistinguishable from a clean repository. Each tool now reports `ok | missing |
error` with its stderr tail in the job summary, and `swarm scan` exits nonzero
when no scanner produced output. An empty backlog is only good news if something
actually looked.

**And a second scanner used to refile work already tracked.** The dedupe key was
`sha256(tool|ecosystem|package|version)`, so the same vulnerable pin reported by
`pip-audit` hashed differently from `osv-scanner`'s — three duplicate issues, one
per package. Identity belongs to the *finding*, not the tool that noticed it, so
the tool is out of the key, findings from several tools collapse into one task
(the issue body names every reporter), and issues filed under the old key are
still recognised.

### `semgrep` — code-level findings, adapter shipped, not enabled

`scan.py` parses semgrep JSON into the same `Finding` shape and files it as
`class: security` scoped to the offending file. It is deliberately **not** in
the live workflow's `--tools` list: on a codebase Superset's size `p/python`
produces far more findings than a demo backlog should contain, and the two
code-level security issues in the fork (`yaml.load` on example metadata,
unrestricted `pickle` in the key-value codec) were curated instead so their
acceptance criteria could be written properly. Enabling it is one word in
`swarm-scan.yml`.

### Anything you already own

The adapter boundary is the `Finding` dataclass, not the tool. Snyk, Wiz,
SonarQube, Dependabot alerts, an internal SAST — anything that can be normalised
into `Finding(tool, ecosystem, package, advisory, severity, current_version,
fixed_version, file, summary)` reaches dispatch without touching issue filing,
scheduling, policy or the reconciler. That is the actual pitch: most
organisations already have detection and nothing that closes the loop.

## Deduplication: 40 records → 8 issues

The live scan of the fork returns **40 advisory records** covering **34 distinct
advisory ids** across **8 packages**. Filing one issue per record would file 40
issues, refile them next week, and dispatch several sessions to edit the same
line of the same file.

Collapsing happens three ways:

1. **Per package, not per advisory.** `mcp` has six advisories; it needs one
   bump. Advisory ids beyond the first are folded into the issue body as
   aliases (the title carries `(+5 more)`), and the highest offered fixed
   version wins.
2. **Per package, not per file.** A package pinned in both `base.txt` and
   `development.txt` needs one PR touching both, so every affected file is
   listed in the issue's `touch_scope` — which is also what stops two sessions
   racing on `requirements/`.
3. **Per finding, not per scanner.** Two tools reporting the same vulnerable pin
   is one piece of work.

Identity is `sha256(ecosystem|package|current_version)`, recorded in the issue
body as `<!-- swarm-finding: … -->`. Re-running the scan finds the fingerprint on
the existing issue and files nothing. Titles change; fingerprints do not.

## Classification is what makes remediation dispatchable

A finding is not yet a task. `scan.py` compares the current and fixed versions
and assigns a class, because the class decides everything downstream:

| Class | When | ACU limit | Merge tier |
|---|---|---|---|
| `dep-bump-patch` | fixed version shares the major | low | auto-merge on green CI + clean review, ≤3 files |
| `dep-bump-major` | major version changes | higher | auto-merge only on a clean Devin Review |
| `security` (semgrep / curated) | code-level finding | higher | **always human** |

The distinction is the entire argument for using an agent here. A patch bump is
a text substitution; Renovate has solved it. A major bump breaks call sites, and
the work is "read the changelog, find the callers, fix them, run the tests,
explain what you did" — which is a person's afternoon, or a Devin session.

Two real examples from the live run:

- **#16 pytest 7.4.4 → 9.0.3** — the pin could not move alone: `pytest-asyncio
  0.23.8` caps `pytest<9`, so the session bumped both, verified, and said so in
  its structured output. A version-bumping bot either fails here or opens a PR
  that cannot resolve.
- **#12 setuptools 80.9.0 → 83.0.0** — the session came back `wont_fix` with
  reasoning, rather than forcing a bump. A refusal with a reason is a valid
  output; a bot has no way to produce one.

## What the scan cannot do, and what would come next

- No runtime/config scanning (secrets, IaC, container base images). Same adapter
  shape, so it is a parser away.
- No frontend dependency scan yet: Superset's `package.json` is in scope for
  osv-scanner but was left out to keep the demo backlog Python-only.
- No reachability analysis. Every advisory is treated as worth fixing; a
  reachability signal (Snyk, Endor) would feed the class and the merge tier
  instead of the severity string.

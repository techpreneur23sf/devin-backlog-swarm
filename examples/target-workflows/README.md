# Target-repo workflows

The five workflows the swarm needs in the repository it maintains. They are
copies of what runs in `techpreneur23sf/apache-superset`, unedited.

Each is an event bus and nothing more: it pulls
`ghcr.io/techpreneur23sf/devin-backlog-swarm:main` and runs one `swarm`
command. All logic, policy and state handling lives in the image, so adopting
the swarm in another repository is these files plus three secrets — and moving
it to GitLab or Jenkins is a rewrite of these files only.

| Workflow | Trigger | Command |
|---|---|---|
| `swarm-dispatch.yml` | issue labelled `devin:auto` | `swarm dispatch --issue N` |
| `swarm-nightly.yml` | cron, or manual with `max_sessions` | `swarm nightly` |
| `swarm-reconcile.yml` | every 15 min, PR closed, manual | `swarm reconcile --publish` |
| `swarm-scan.yml` | cron, manual | `swarm scan --file-issues` |
| `swarm-verify.yml` | pull request | `swarm verify --pr N --post-status` |

Secrets: `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `SWARM_GITHUB_TOKEN`.
`swarm-verify.yml` needs only the GitHub token — the PR gate must not depend on
Devin being reachable.

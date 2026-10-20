# Plane software factory

The factory polls Plane for work items in `Ready for Agent`, runs each item
through an unattended SimpleCode pipeline on an isolated branch, and opens a
draft GitHub pull request after QA passes.

It does not merge pull requests or write to the default branch.

## Prerequisites

- A Plane API key with access to the configured workspace
- `git` and an authenticated `gh` CLI
- An installed agent runtime (`claude`, `cursor`, `codex`, `opencode`, or `pi`)
- The four Plane workflow states used below in every mapped project
- A clean local clone area (the default is `~/.simplecode/repos`)

## Configure

Create `factory.yaml`:

```yaml
plane:
  base_url: http://localhost:8180
  workspace_slug: engineering
  api_key_env: PLANE_API_KEY

poll_interval: 30
states:
  ready: Ready for Agent
  in_progress: In Progress
  in_review: In Review
  needs_attention: Needs Attention

pipeline: ../examples/factory-ticket-delivery
output_dir: ../pipeline-output
runtime: claude
timeout: 86400
# model: optional-runtime-model
# docker_image: simplecode-agent:latest

projects:
  ENG:
    repo: your-org/backend
    base_branch: main
  WEB:
    repo: your-org/frontend
```

To run each delivery attempt in an ephemeral Coder workspace, add:

```yaml
workspace:
  provider: coder
  coder:
    url: http://coder.localhost:7080
    token_env: CODER_SESSION_TOKEN
    template: simplecode-factory
    provision_timeout: 600
    cleanup_timeout: 600
    stop_after: 2h
    max_bundle_mb: 512
```

Export a Coder API token for the dedicated factory user. The token authenticates
the local Coder CLI; it is not copied into workspaces. The factory transfers the
managed branch with Git bundles, runs agents over `coder ssh`, imports the
committed result, and publishes to GitHub from the factory host. Do not combine
this provider with `docker_image`; the Coder template owns workspace isolation.
See [Coder workspaces](coder-workspaces.md) for the local Keycloak-backed stack.

Paths are resolved relative to `factory.yaml`. Project keys may be Plane project
identifiers such as `ENG` or exact project UUIDs. Repository values are an
allowlist; a Plane work item cannot override them.

Keep the API key out of YAML:

```bash
export PLANE_API_KEY='plane_api_...'
```

## Run

Exercise one polling cycle first:

```bash
simplecode factory --config factory.yaml --once
```

Run continuously:

```bash
simplecode factory --config factory.yaml
```

## Event-driven mode

Event-driven mode uses signed Plane and GitHub webhooks as the fast path, a
durable database task queue, and a slow Plane reconciliation pass for missed
deliveries. PostgreSQL is required when the API, workers, and reconciler run as
separate processes; SQLite supports the single-process development role.

Add the terminal Plane state and event settings to `factory.yaml`:

```yaml
states:
  ready: Ready for Agent
  in_progress: In Progress
  in_review: In Review
  done: Done
  needs_attention: Needs Attention

events:
  enabled: true
  bind: 127.0.0.1
  port: 8091
  plane_secret_env: PLANE_WEBHOOK_SECRET
  github_secret_env: GITHUB_WEBHOOK_SECRET
  reconcile_interval: 300
  task_lease_seconds: 60
  max_attempts: 5
  retry_base_seconds: 10
```

Set both secrets in the environment, then run the roles against the same
PostgreSQL database:

```bash
simplecode factory -c factory.yaml --role api
simplecode factory -c factory.yaml --role worker
simplecode factory -c factory.yaml --role reconciler
```

For local development, `--role standalone` starts all three roles and permits
SQLite. The API binds only to loopback; publish `/webhooks/plane` and
`/webhooks/github` through an HTTPS reverse proxy. Do not expose the dashboard.

Configure Plane webhook v2 for `workitem.updated`. Configure GitHub for
`pull_request`, `pull_request_review`, and `check_suite`. Intake verifies HMAC
signatures, stores only identifiers and a payload hash, and returns only after
the delivery and its task are committed. Duplicate delivery identifiers are
accepted without creating duplicate work.

Workers serialize delivery within one repository while allowing different
repositories to run concurrently. A merged PR moves the Plane item to `done`;
a PR closed without merging moves it to `needs_attention`. CI and review events
update progress only and do not start automatic repair work.

Operator commands remain explicit and CLI-only:

```bash
simplecode factory-jobs list
simplecode factory-jobs show WORKSPACE:PROJECT:ITEM
simplecode factory-jobs cancel WORKSPACE:PROJECT:ITEM
simplecode factory-jobs retry WORKSPACE:PROJECT:ITEM
simplecode factory-workspaces list
simplecode factory-workspaces cleanup WORKSPACE_NAME -c factory.yaml
simplecode factory-events replay plane EVENT_ID
```

Cancellation terminates the active agent subprocess, preserves its worktree,
and moves the Plane item to Needs Attention. Retry is allowed only for failed
or cancelled jobs that did not publish a pull request. Move the Plane item back
to `Ready for Agent` before running the retry command; otherwise the retry stays
failed without discarding its job history.

Continuous mode starts the local operations dashboard at
<http://127.0.0.1:8090>. Use `--dashboard-port PORT` to select another
loopback port, or `--no-dashboard` to run only the poller:

```bash
simplecode factory --config factory.yaml --dashboard-port 9090
simplecode factory --config factory.yaml --no-dashboard
```

The dashboard is not started by `--once`.

Set `SIMPLECODE_DATABASE_URL` or pass `--database-url` to use PostgreSQL or a
specific SQLite file. The default is `~/.simplecode/simplecode.db`.

## Dashboard and progress events

Factory state and progress events are written to the configured database before
the dashboard reads them. The page refreshes its recent-job snapshot and the
selected job every two seconds, so a browser refresh or dashboard restart does
not lose progress.

The dashboard shows the claim, checkout/setup, delivery agents (`explorer`,
`builder`, `adversarial`, `fixer`, and `qa`), verdict, publication, Plane review
synchronization, recovery, completion, and failure boundaries. Prompt starts
and finishes are also durable events. Ticket bodies, Plane comments, prompts,
agent output, stdout, model reasoning, and event metadata are not returned by
the dashboard API.

The factory maintains one `Factory Progress` comment on each Plane work item.
It creates the comment at the first visible milestone and updates the same
comment at factory milestones and agent boundaries. Prompt events do not update
Plane. Transient comment errors do not fail the job or create another comment;
if the stored comment was deleted in Plane, the factory creates one replacement
and remembers its new ID.

## Work-item lifecycle

1. Move a mapped work item to `Ready for Agent`.
2. The factory claims it once and moves it to `In Progress`.
3. The `factory-ticket-delivery` pipeline explores, builds, reviews, fixes, and
   performs QA in a managed worktree.
4. QA must end `qa/output.md` with exactly `FACTORY_VERDICT: PASS`.
5. The factory verifies a clean committed branch, pushes it, and opens a draft
   PR.
6. Plane receives the PR link and moves to `In Review`.

`FACTORY_VERDICT: BLOCKED`, a missing verdict, uncommitted changes, no commits
ahead of the base branch, or any execution/publishing failure moves the item to
`Needs Attention`. The worktree is preserved under the repository's
`.simplecode/worktrees/` directory.

If the draft PR exists but Plane cannot move to `In Review`, the job remains
`published` and the next poll retries only the Plane synchronization. It does
not create another PR or move the item to `Needs Attention`.

## Recovery

Claims are durable and prevent duplicate work after repeated polls. Every poll,
including `--once`, first reconciles interrupted jobs. An interrupted
pre-publication job is not rerun automatically because its previous process may
have changed the branch; it moves to `Needs Attention`. A recorded `published`
job retries the `In Review` transition and completes without creating another
PR.

SQLite file locking or a PostgreSQL advisory lock serializes recovery and work,
so multiple factory processes using the same database cannot recover a live job.
All processes managing the same repositories must use the same factory
database.

In legacy polling mode, retry an interrupted pre-publication item only after
diagnosing or removing its old factory-job record. Event-driven mode instead
provides the explicit `factory-jobs retry` command described above and creates a
new attempt branch.

## Security boundary

- Plane API keys are read from an environment variable and are never placed in
  generated ticket files.
- Only configured repositories can be cloned or published.
- Existing clones must have the expected GitHub origin.
- GitHub and git operations use argument arrays without a shell.
- The publisher refuses dirty worktrees, the default branch, and branches with
  no commits ahead of `origin/<base>`.
- Human review and merge remain in GitHub.
- The dashboard is read-only and the CLI binds it only to `127.0.0.1`.
- The dashboard has no authentication or TLS. Any local user or process that
  can reach its port can read work-item references, titles, repositories,
  branches, session IDs, progress, PR links, and sanitized errors.
- Do not expose the dashboard through a public/container bind, shared tunnel,
  or reverse proxy without adding authentication and transport security.

Plane webhooks can replace polling later. Polling is the default for the local
self-hosted stack because Plane rejects private webhook targets unless its
private-URL escape hatch is explicitly enabled.

## Troubleshooting

### Dashboard does not start

The factory prints `Warning: dashboard could not start; factory will continue.`
and keeps polling. The usual cause is another process using the requested port.
Choose a free port with `--dashboard-port`, or use `--no-dashboard`. The warning
intentionally omits exception details that could contain credentials.

### Dashboard says DATA OFFLINE

Check that the `simplecode factory` process is still running and that the
configured SQLite file or PostgreSQL service is reachable. `GET /health`
returns `200` only when the server can open its store. The browser retries every
two seconds.

### Jobs or events appear stale

Confirm that every factory process managing the repositories uses the same
`--database-url` or `SIMPLECODE_DATABASE_URL`. The dashboard reads only that
durable store. It displays the 100 most recently active jobs and up to 500
events for the selected job.

### Plane shows no progress comment

Comment writes are best-effort so they cannot stop delivery. Verify that the
Plane API key can create and update work-item comments. Durable events remain
available in the local dashboard even when Plane comment synchronization
fails.

### Published job does not complete

If a draft PR exists but Plane could not move the item to `In Review`, the job
stays `published`. The next poll retries only that Plane transition; it does
not publish another PR.

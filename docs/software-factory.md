# Plane software factory

The factory polls Plane for work items in `Ready for Agent`, runs each item
through an unattended SimpleCode pipeline on an isolated branch, and opens a
draft GitHub pull request after QA passes.

It does not merge pull requests or write to the default branch.

## Prerequisites

- A Plane API key with access to the configured workspace
- `git` and an authenticated `gh` CLI
- An installed agent runtime (`claude`, `cursor`, `codex`, or `opencode`)
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

To retry an item, diagnose or remove the old factory-job record and choose a new
branch deliberately. Automatic retry tooling is intentionally outside the MVP.

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

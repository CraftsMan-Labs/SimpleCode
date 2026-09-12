# GitHub Webhook Integration

Run a local webhook server that triggers SimpleCode pipelines when you `@simplecode` on a GitHub issue. Reply to the agent's comments to give feedback and continue the pipeline.

## Flow

```
1. Comment `@simplecode triage` on issue #42
2. Webhook fires → server starts the triage pipeline
3. Pipeline checkpoints → server posts agent output as comment → adds `simplecode:waiting` label
4. You review the agent's work
5. Reply with your feedback → agent gets it and pipeline continues to the next step
6. If the pipeline hits another gate, repeat from step 3
7. Pipeline completes → server posts completion comment → adds `simplecode:done` label
```

## Setup

### 1. Start the server

```bash
simplecode serve \
  --port 8080 \
  --repos owner/repo1,owner/repo2 \
  --pipeline-map '{"triage":"examples/github-issue-triage","feature":"examples/github-issue-feature-creator"}' \
  --github-user your-github-username \
  --default-pipeline examples/github-issue-feature-creator \
  -r claude \
  -t 86400
```

`--repos` polls GitHub every 30s — no webhook or tunnel needed. Webhooks are optional for faster delivery; both paths deduplicate by comment ID.

Repos are cloned into `~/.simplecode/repos/<owner>/<repo>/` on first trigger and pulled on subsequent triggers. Override the base directory with `--repos-dir`.

**Single-repo mode** — lock all pipelines to one working directory:

```bash
simplecode serve \
  --port 8080 \
  --pipeline-map '{"triage":"examples/github-issue-triage"}' \
  --github-user your-github-username \
  -r claude \
  -w /path/to/target-repo \
  -t 86400
```

### 2. Expose your server (webhook only)

Skip this if you use `--repos`. Otherwise the server needs to be reachable from GitHub:

```bash
ngrok http 8080
# Copy the https URL, e.g. https://abc123.ngrok.io
```

### 3. Configure the GitHub webhook

In your repository: **Settings → Webhooks → Add webhook**

| Field | Value |
|-------|-------|
| Payload URL | `https://abc123.ngrok.io/webhook` |
| Content type | `application/json` |
| Secret | (optional, see below) |
| Events | Select: **Issues** and **Issue comments** |

## Usage

### Starting a pipeline

Comment on any issue with `@simplecode` followed by a keyword:

```
@simplecode triage
@simplecode feature
```

The keyword maps to a pipeline directory via `--pipeline-map`. If the keyword isn't recognized, the bot replies with the available options.

Pipelines can also be triggered by **assigning an issue** to the user specified in `--github-user` (uses `--default-pipeline`).

### Interacting with agents

When the pipeline checkpoints, the server posts a comment on the issue:

| You do | What happens |
|--------|-------------|
| **Reply with any text** | Sent as feedback; the pipeline stays at this checkpoint |
| **`/approve` or `/continue`** | Approves the gate and advances to the next agent |
| **`/stop`** | Stops the entire pipeline |
| **`@simplecode --help`** | Lists all agents in the pipeline |
| **`@simplecode --list`** | Same as `--help` |

### Example

```
You: @simplecode triage

Bot: ## Pipeline Checkpointed
     Agent: investigator (1/3)
     Session: a1b2c3d4
     ...

You: Also handle the edge case in parse()

→ Agent receives feedback and stays at the investigator checkpoint

You: /approve

→ Pipeline advances to executor, then checkpoints at reviewer

Bot: ## Pipeline Checkpointed
     Agent: reviewer (3/3)
     ...

You: /approve

→ Pipeline completes

Bot: ## Pipeline Complete
     Agents completed: 3/3
```

### Listing agents

Comment `@simplecode --help` on any issue:

```
## @simplecode — github-issue-triage

| # | Agent        | Gate  | Permissions |
|---|--------------|-------|-------------|
| 1 | investigator | human | write       |
| 2 | executor     | —     | write       |
| 3 | reviewer     | human | read-only   |
```

## CLI options

| Option | Env var | Default | Description |
|--------|---------|---------|-------------|
| `--port` | — | 8080 | Port to listen on |
| `--repos` | — | — | Comma-separated repos to poll for `@simplecode` mentions (every 30s) |
| `--webhook-secret` | `SIMPLECODE_WEBHOOK_SECRET` | — | GitHub webhook secret for HMAC validation |
| `--pipeline-map` | — | (required) | JSON mapping of keywords to pipeline directories |
| `--default-pipeline` | — | — | Pipeline directory for assignment triggers |
| `--github-user` | `GITHUB_USER` | — | GitHub username for assignment matching |
| `--database-url` | `SIMPLECODE_DATABASE_URL` | SQLite default | Database for session persistence |
| `-r, --runtime` | — | claude | AI runtime (claude, codex, cursor, opencode) |
| `-w, --working-dir` | — | (none) | Working directory for agents (omit for multi-repo auto-clone) |
| `--repos-dir` | — | `~/.simplecode/repos/` | Base directory for auto-cloned repos |
| `-o, --output` | — | ./pipeline-output | Output directory for artifacts |
| `-t, --timeout` | — | 86400 | Timeout per agent in seconds |
| `-m, --model` | — | — | Default model override |
| `--permissions` | — | write | Default permission level |

## Webhook secret (recommended)

```bash
export SIMPLECODE_WEBHOOK_SECRET=$(openssl rand -hex 20)
simplecode serve --pipeline-map '...' ...
```

Set the same secret in GitHub webhook settings. Without a secret, the server accepts all POST requests.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/webhook` | GitHub webhook receiver |
| GET | `/health` | Health check (returns `{"status":"ok"}`) |

## Labels

The server manages these labels automatically:

| Label | Meaning |
|-------|---------|
| `simplecode:waiting` | Pipeline is checkpointed, waiting for your reply |
| `simplecode:done` | Pipeline completed successfully |
| `simplecode:stopped` | Pipeline was stopped via `/stop` |

## Troubleshooting

**Server not receiving webhooks:** Check ngrok is running, the URL in GitHub settings is correct, and you selected the right events (Issues + Issue comments).

**`@simplecode triage` not working:** Verify `triage` is a key in your `--pipeline-map` JSON and the pipeline directory exists.

**Resume not working:** Check that the issue has the `simplecode:waiting` label and that a previous comment contains the session marker.

**`gh` CLI errors:** The server uses `gh` to post comments and manage labels. Run `gh auth login` on the machine running the server.

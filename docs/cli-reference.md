# CLI Reference

## simplecode run

Run a pipeline by **name** (from `.simplecode`) or by path.

```bash
uv run simplecode run <name-or-path> [OPTIONS]
uv run simplecode run -f <pipeline.yaml> [OPTIONS]
```

| Flag / arg | Short | Default | Description |
|------|-------|---------|-------------|
| `PIPELINE` | | | Name under `.simplecode/pipelines/`, or a path |
| `--file` | `-f` | none | Explicit path to pipeline.yaml |
| `--runtime` | `-r` | `claude` | Agent CLI: `claude`, `cursor`, `codex`, `opencode` |
| `--working-dir` | `-w` | `.` | Repo the agent operates on (also locates project `.simplecode`) |
| `--output` | `-o` | `./pipeline-output` | Output directory for artifacts |
| `--timeout` | `-t` | `300` | Default timeout per agent (seconds) |
| `--model` | `-m` | runtime default | Default model for all agents |
| `--permissions` | | `write` | Default: `read-only`, `write`, `full` |
| `--input` | `-i` | none | Task/context files injected into the first agent's prompt (repeatable) |
| `--gate-mode` | | `stdin` | `stdin` (interactive), `file` (cloud/CI), or `checkpoint` (save and exit) |
| `--gate-dir` | | auto | Directory for file-based gate artifacts |
| `--database-url` | | SQLite default | Session store (`SIMPLECODE_DATABASE_URL`) |

### Examples

```bash
# By name (project or global .simplecode)
uv run simplecode run feature-dev -r claude -w . -i TASK.md

# Explicit path (still supported)
uv run simplecode run -f examples/plan-execute-review/pipeline.yaml -r claude -w .

# Full-featured run
uv run simplecode run feature-dev \
  -r opencode \
  -w ~/projects/myapp \
  -t 86400 \
  -m opus \
  --permissions write \
  -i TASK.md \
  -i CONTEXT.md \
  -o ./output

# Cloud/CI mode with file-based gates
uv run simplecode run feature-dev \
  -r claude \
  -w ~/projects/myapp \
  --gate-mode file \
  --gate-dir ./gates

# Verbose logging
uv run simplecode -v run feature-dev -r claude -w .
```

## simplecode init

Scaffold a pipeline into `.simplecode/pipelines/<name>/` (project) or `~/.simplecode/pipelines/<name>/` (global).

```bash
uv run simplecode init <name> [--agents N] [--global] [--path DIR]
```

| Argument/Flag | Default | Description |
|---------------|---------|-------------|
| `name` | `my-pipeline` | Pipeline name |
| `--agents` | `3` | Number of agents to scaffold |
| `--global` | off | Scaffold under `~/.simplecode/pipelines/` |
| `--path` | none | Explicit directory (legacy escape hatch) |

### Examples

```bash
# Project pipeline (recommended)
uv run simplecode init feature-dev --agents 5

# Shared across all projects
uv run simplecode init feature-dev --global --agents 5

# Legacy flat directory
uv run simplecode init feature-dev --path ./my-pipeline
```

Default agent names (in order): `planner`, `executor`, `reviewer`, `review-tasks`, `fixer`. Extra agents are named `agent-6`, `agent-7`, etc.

## simplecode resume

Resume a checkpointed pipeline from the session store.

```bash
uv run simplecode resume -s <session-id> [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--session` / `-s` | Session ID (required) |
| `--approve` | Approve the gate and continue |
| `--feedback` | Send feedback and stay at the checkpoint |
| `--feedback-file` | Read feedback from a file |
| `--reject` | Stop the pipeline |
| `--gate-mode` | Mode for later gates (`stdin`, `file`, `checkpoint`) |
| `--database-url` | Session store URL |

```bash
simplecode resume -s a1b2c3d4 --approve
simplecode resume -s a1b2c3d4 --feedback "Add tests for parse()"
simplecode resume -s a1b2c3d4 --reject
```

## simplecode sessions

List stored pipeline sessions.

```bash
uv run simplecode sessions [--status waiting]
```

Statuses: `running`, `waiting`, `resumed`, `completed`, `rejected`.

## simplecode serve

Start the GitHub webhook server, optionally polling repos for `@simplecode` mentions.

```bash
uv run simplecode serve --pipeline-map '{"triage":"examples/github-issue-triage"}' [OPTIONS]
```

See [GitHub webhooks](github-webhooks.md) for the full option table.

## simplecode list

List discovered pipelines, or show detail for one.

```bash
uv run simplecode list
uv run simplecode list <name>
uv run simplecode list -f <pipeline.yaml>
```

### Example output

```
NAME                         SCOPE      PATH
--------------------------------------------------------------------------------
feature-dev                  project    /repo/.simplecode/pipelines/feature-dev
```

Detail view:

```
Pipeline: feature-dev (project)
Path:     /repo/.simplecode/pipelines/feature-dev/pipeline.yaml
Description: Plan, build, review, and fix a feature
Setup: setup.sh

  1. planner: 3 prompts, perm=read-only model=opus [gate: human]
  2. executor: 2 prompts, perm=write model=sonnet
  3. reviewer: 3 prompts, perm=read-only model=opus [gate: human]
  4. review-tasks: 1 prompts, perm=read-only
  5. fixer: 2 prompts, perm=write
```

## Global flags

| Flag | Short | Description |
|------|-------|-------------|
| `--version` | | Show version and exit |
| `--verbose` | `-v` | Enable debug logging (place before the subcommand) |
| `--help` | | Show help |

```bash
uv run simplecode --version
uv run simplecode -v run -f pipeline.yaml -r claude -w .
```

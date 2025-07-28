# CLI Reference

## simplecode run

Run a pipeline from a YAML file.

```bash
uv run simplecode run -f <pipeline.yaml> [OPTIONS]
```

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--file` | `-f` | *(required)* | Path to pipeline.yaml |
| `--runtime` | `-r` | `claude` | Agent CLI: `claude`, `cursor`, `codex`, `opencode` |
| `--working-dir` | `-w` | `.` | Repo the agent operates on |
| `--output` | `-o` | `./pipeline-output` | Output directory for artifacts |
| `--timeout` | `-t` | `300` | Default timeout per agent (seconds) |
| `--model` | `-m` | runtime default | Default model for all agents |
| `--permissions` | | `write` | Default: `read-only`, `write`, `full` |
| `--input` | `-i` | none | Task/context files injected into the first agent's prompt (repeatable) |
| `--gate-mode` | | `stdin` | `stdin` (interactive) or `file` (cloud/CI) |
| `--gate-dir` | | auto | Directory for file-based gate artifacts |

### Examples

```bash
# Basic run with claude
uv run simplecode run -f my-pipeline/pipeline.yaml -r claude -w ~/projects/myapp

# Full-featured run
uv run simplecode run \
  -f my-pipeline/pipeline.yaml \
  -r opencode \
  -w ~/projects/myapp \
  -t 86400 \
  -m opus \
  --permissions write \
  -i TASK.md \
  -i CONTEXT.md \
  -o ./output

# Cloud/CI mode with file-based gates
uv run simplecode run \
  -f my-pipeline/pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  --gate-mode file \
  --gate-dir ./gates

# Verbose logging
uv run simplecode -v run -f my-pipeline/pipeline.yaml -r claude -w .
```

## simplecode init

Scaffold a new pipeline directory.

```bash
uv run simplecode init <name> [--agents N]
```

| Argument/Flag | Default | Description |
|---------------|---------|-------------|
| `name` | `my-pipeline` | Directory name to create |
| `--agents` | `3` | Number of agents to scaffold |

### Examples

```bash
# Create a 5-agent pipeline
uv run simplecode init feature-dev --agents 5

# Create a simple 2-agent pipeline
uv run simplecode init quick-review --agents 2
```

Default agent names (in order): `planner`, `executor`, `reviewer`, `review-tasks`, `fixer`. Extra agents are named `agent-6`, `agent-7`, etc.

## simplecode list

Show agents and prompt counts for a pipeline.

```bash
uv run simplecode list -f <pipeline.yaml>
```

### Example output

```
Pipeline: feature-dev
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

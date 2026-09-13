# Pipeline YAML Reference

Every pipeline is a YAML file + a `prompts/` directory. The YAML defines agents and their configuration. The prompts directory holds numbered markdown files (1.md, 2.md, ...) that each agent executes in order.

## Full example

```yaml
name: feature-dev
description: Plan, build, review, and fix a feature

setup: setup.sh                  # run before any agent starts (optional)

agents:
  - id: planner
    prompts: prompts/planner/    # directory with 1.md, 2.md, ...
    gate: human                  # pause for human review after this agent
    permissions: read-only
    model: opus                  # use a specific model for this agent
    timeout: 86400               # 24 hours

  - id: executor
    prompts: prompts/executor/
    permissions: write
    model: sonnet
    timeout: 86400

  - id: reviewer
    prompts: prompts/reviewer/
    gate: human
    permissions: read-only
    model: opus

  - id: review-tasks
    prompts: prompts/review-tasks/
    permissions: read-only

  - id: fixer
    prompts: prompts/fixer/
    permissions: write
    timeout: 86400
```

## Fields

### Top-level

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | no | filename | Pipeline name (shown in output) |
| `description` | no | `""` | What this pipeline does |
| `setup` | no | none | Path to a setup script, relative to the YAML file |
| `branch_strategy` | no | `head` | Git isolation: `head` (host cwd), `branch` (named worktree), or `merge-to-head` (temp worktree, merge on success) |
| `branch` | when strategy is `branch` | none | Explicit branch name for the worktree |
| `docker_image` | no | none | If set, start one long-lived container and run agents via `docker exec` |
| `agents` | **yes** | — | List of agent definitions |

### Per agent

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `id` | **yes** | — | Unique identifier for this agent |
| `prompts` | **yes** | — | Path to prompts directory (relative to YAML file) |
| `gate` | no | none | `"human"` to pause for human review after this agent |
| `permissions` | no | `"write"` | `"read-only"`, `"write"`, or `"full"` |
| `model` | no | CLI default | Model override (e.g. `opus`, `sonnet`, `gpt-5.4`) |
| `timeout` | no | `300` | Timeout in seconds for this agent |
| `runtime` | no | CLI default | Override the runtime for this specific agent |

## Prompts directory

Each agent's `prompts` field points to a directory containing numbered markdown files:

```
prompts/planner/
├── 1.md     ← runs first (starts a new CLI session)
├── 2.md     ← runs second (resumes the same session)
└── 3.md     ← runs third (resumes the same session)
```

Rules:
- Files must be named `1.md`, `2.md`, `3.md`, etc.
- Numbering must start at 1 with no gaps
- Non-numbered files (like README.md) are ignored
- All prompts in one agent share the same CLI session

## Setup script

The `setup` field points to a script that runs before any agent starts. Use it to install tools your agents need.

```bash
#!/usr/bin/env bash
set -euo pipefail

pip install playwright
playwright install chromium
npm install -g typescript
```

The script:
- Runs from the effective working directory (`-w`, or the worktree when isolation is on)
- Must exit 0 or the pipeline aborts
- Has a 10-minute timeout
- Must be executable (`chmod +x setup.sh`)
- Always runs on the host (even when `docker_image` is set)

## Workspace isolation

Optional fields that move agent writes off your current checkout (or into Docker) without changing how agents and gates work. Full guide: [Workspace isolation](workspace.md).

```yaml
name: feature-dev
branch_strategy: merge-to-head   # or: head | branch
# branch: agent/issue-42         # required when branch_strategy: branch
# docker_image: my-agent:latest  # optional long-lived container
agents:
  - id: executor
    prompts: prompts/executor/
```

| Strategy | What it does |
|----------|--------------|
| `head` | Host working directory (default) |
| `branch` | Named worktree under `.simplecode/worktrees/`; kept after the run |
| `merge-to-head` | Temp worktree; merges to the host branch only on a clean successful complete |

Only committed changes merge. Dirty or conflicted trees are preserved and recorded in `pipeline.json`. CLI: `--branch-strategy`, `--branch`, `--docker-image`.

## Context flow

```
[input files (-i)] → Agent 1 → output.md → Agent 2 → output.md → Agent 3 → ...
```

- Input files (`-i TASK.md`) are injected as context into the first agent's first prompt
- Each agent's final output (`output.md`) is passed as context to the next agent's first prompt
- Prompts 2+ within the same agent don't need explicit context — they share the session history

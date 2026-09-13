# Workspace isolation

By default, SimpleCode runs agents straight in whatever directory you pass with `-w`. That is fine for local tinkering. It is less fine when an unattended agent can leave half-finished commits on your current branch, or when you want the same pipeline to run inside Docker without rewriting the YAML.

Workspace isolation is the opt-in answer. Same sequential pipeline, same gates, same prompts. The only change is *where* the agent writes, and optionally *how* the process is launched.

These ideas come from [Sandcastle](https://github.com/mattpocock/sandcastle) (Matt Pocock). We borrowed the useful parts and kept them as flat config fields, not a second orchestration framework.

## Quick pick

| You want… | Use |
|-----------|-----|
| Current behaviour (no isolation) | Do nothing. Default is `head`. |
| A named branch you can reopen later | `branch_strategy: branch` + `branch: agent/…` |
| Throwaway work that merges back on success | `branch_strategy: merge-to-head` |
| Agents inside a container | `docker_image: your-image:tag` (combine with any strategy) |

CLI flags override YAML for a single run:

```bash
uv run simplecode run feature-dev \
  --branch-strategy merge-to-head \
  -w ~/projects/myapp \
  -i TASK.md

uv run simplecode run feature-dev \
  --branch-strategy branch \
  --branch agent/issue-42 \
  --docker-image my-agent:latest
```

## Branch strategies

### `head` (default)

Agents use the host working directory. No worktree, no merge step. If you never set `branch_strategy`, this is what you get.

### `branch`

SimpleCode creates (or reuses) a git worktree under:

```
.simplecode/worktrees/<branch-with-slashes-flattened>/
```

Commits land on the named branch. The worktree stays around after the run so you can resume a checkpoint, re-run the pipeline, or inspect the tree by hand. We never auto-delete named branch worktrees.

If the branch already exists and the worktree is clean, we reuse it. If it is dirty, we still reuse it and log a warning. Pick a different branch name if git already has that branch checked out in the main tree or some other worktree.

### `merge-to-head`

Creates a temporary branch (`simplecode/<session>/…`) in a worktree. The agent works there. When the pipeline finishes with `status: complete` **and** the worktree is clean (committed only), SimpleCode merges into the host's current branch and removes the temp worktree and branch.

If anything is off (dirty tree, merge conflict, dirty host, pipeline rejected/checkpointed/failed), the worktree is left alone. Check `pipeline.json` for `merge_result` and `preserved_worktree`.

Important: we only merge **commits**. Uncommitted edits are never auto-committed. If the agent forgot to commit, you get a preserved worktree, not a surprise merge.

## Docker

Set `docker_image` (YAML) or `--docker-image` (CLI). SimpleCode:

1. Starts one long-lived container for the whole pipeline run
2. Bind-mounts the effective worktree (or host cwd under `head`) at `/workspace`
3. Runs every agent CLI as `docker exec -i -w /workspace <container> …`

The image must already have your agent binary (`claude`, `agent`, `codex`, or `opencode`) and any tools the prompts call. We ship an optional starter at [`docker/agent/Dockerfile`](../docker/agent/Dockerfile) (`make agent-image`).

If Docker network `simplecode-stack` is up (see [Shared agent stack](agent-stack.md)), the run container joins it automatically so agents can reach 9Router / Headroom by service name. Override with `SIMPLECODE_DOCKER_NETWORK`, or set it empty to skip.

The container is torn down when the run exits, including at a checkpoint. Resume starts a new container against the same worktree path stored in the session DB.

`setup.sh` still runs on the **host**, against the effective working directory. It is not executed inside the container.

## Idle timeouts

Agent subprocesses stream stdout and stderr. The per-agent `timeout` is an idle silence window: any output resets the clock. A chatty agent can run for hours. A hung agent that goes quiet dies after `timeout` seconds (terminate, then kill if needed).

That is a behaviour change from a pure wall-clock timeout. It matches how long coding agents actually behave.

## Resume and checkpoints

When you use `gate_mode: checkpoint` (or webhook-driven runs), the session store records `branch_strategy`, `branch`, `worktree_path`, `docker_image`, and `host_repo`. Resume reopens the same worktree. Ephemeral `merge-to-head` trees are kept across checkpoints until a successful complete run merges them.

## Recovery cheat sheet

| Situation | What happens |
|-----------|----------------|
| Pipeline complete, clean merge-to-head worktree | Merge to host branch, delete temp worktree |
| Dirty worktree at end | Preserved; `merge_result: preserved-dirty` |
| Merge conflict | Merge aborted; worktree preserved |
| Host working tree dirty | No merge; `merge_result: dirty-host` |
| Named `branch` strategy | Worktree always kept |
| Checkpoint / reject / crash | Worktree preserved when applicable; container removed |

Look at `pipeline-output/<session>/pipeline.json` for `branch`, `worktree_path`, `merge_result`, and `preserved_worktree`.

## YAML sketch

```yaml
name: feature-dev
description: Plan and implement on an isolated branch

branch_strategy: merge-to-head
# docker_image: my-agent:latest

agents:
  - id: planner
    prompts: prompts/planner/
    gate: human
    permissions: read-only

  - id: executor
    prompts: prompts/executor/
    permissions: write
```

For a durable PR branch instead of merge-back:

```yaml
branch_strategy: branch
branch: agent/oauth-login
```

## What we deliberately skipped

This slice does not include Sandcastle's prompt `!command` expansion, structured output schemas, session fork fan-out, or cloud sandbox providers. Those can land later without undoing this design. The pipeline stays sequential and file-based.

## See also

- [Pipeline YAML](pipeline-yaml.md) — field reference
- [CLI reference](cli-reference.md) — `--branch-strategy`, `--branch`, `--docker-image`
- [Runtimes](runtimes.md) — streaming, idle timeout, Docker exec details
- [Output artifacts](output-artifacts.md) — `pipeline.json` fields

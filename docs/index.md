# SimpleCode Documentation

SimpleCode orchestrates AI coding agents (Claude, Cursor, Codex, OpenCode) through repeatable, multi-step pipelines with human-in-the-loop checkpoints.

## How it works

1. You define a **pipeline** — a sequence of agents, each with numbered prompts
2. Each agent runs its prompts in a single CLI session (non-interactive)
3. At **gates**, the pipeline pauses for your review and feedback
4. Your feedback goes to the **same session** — the agent has full context
5. Type **approve** or **PNS** to advance to the next agent

## Documentation

| Doc | What's in it |
|-----|-------------|
| [Getting Started](getting-started.md) | Install, create, and run your first pipeline in 5 minutes |
| [`.simplecode` directory](simplecode-dir.md) | Project + global pipeline registry, name-based runs |
| [CocoIndex Code](cocoindex.md) | Optional code index/search (`simplecode[code]`) |
| [Pipeline YAML Reference](pipeline-yaml.md) | All YAML config options, prompts, setup scripts |
| [Human Gates](gates.md) | How feedback loops work (stdin, file, and checkpoint) |
| [Python API](python-api.md) | Build and run pipelines from Python code |
| [CLI Reference](cli-reference.md) | All commands and flags |
| [GitHub Webhooks](github-webhooks.md) | Trigger and resume pipelines from GitHub issues |
| [Plane software factory](software-factory.md) | Turn ready Plane work items into draft GitHub PRs |
| [Runtimes](runtimes.md) | Claude, Cursor, Codex, OpenCode — permissions and models |
| [Workspace isolation](workspace.md) | Git worktrees, merge-to-head, optional Docker |
| [Shared agent stack](agent-stack.md) | 9Router + CocoIndex (one compose for all projects) |
| [Output Artifacts](output-artifacts.md) | What gets written and where |
| [Examples](examples.md) | Copy-paste pipelines, including factory chains (spec → tickets, ticket delivery) |

## Quick start

```bash
# Install
uv sync

# Scaffold a project pipeline under .simplecode/pipelines/
uv run simplecode init feature-dev --agents 5

# Edit your prompts
# (edit .simplecode/pipelines/feature-dev/prompts/planner/1.md, ...)

# Run it by name
uv run simplecode run feature-dev \
  -r claude \
  -w /path/to/your/repo \
  -t 86400 \
  -i TASK.md
```

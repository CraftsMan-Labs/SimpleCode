# SimpleCode Documentation

SimpleCode orchestrates AI coding agents (Claude, Cursor, Codex, OpenCode) through repeatable, multi-step pipelines with human-in-the-loop checkpoints.

## How it works

1. You define a **pipeline** — a sequence of agents, each with numbered prompts
2. Each agent runs its prompts in a single CLI session (non-interactive)
3. At **gates**, the pipeline pauses for your review and feedback
4. Your feedback goes to the **same session** — the agent has full context
5. Type **PNS** to advance to the next agent

## Documentation

| Doc | What's in it |
|-----|-------------|
| [Getting Started](getting-started.md) | Install, create, and run your first pipeline in 5 minutes |
| [`.simplecode` directory](simplecode-dir.md) | Project + global pipeline registry, name-based runs |
| [Pipeline YAML Reference](pipeline-yaml.md) | All YAML config options, prompts, setup scripts |
| [Human Gates](gates.md) | How feedback loops work (stdin and file-based) |
| [Python API](python-api.md) | Build and run pipelines from Python code |
| [CLI Reference](cli-reference.md) | All commands and flags |
| [Runtimes](runtimes.md) | Claude, Cursor, Codex, OpenCode — permissions and models |
| [Output Artifacts](output-artifacts.md) | What gets written and where |
| [Examples](examples.md) | Copy-paste pipeline examples (feature dev, QA, bug fix) |

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

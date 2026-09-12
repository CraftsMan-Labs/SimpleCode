# Getting Started

Get a pipeline running in under 5 minutes.

## Install

```bash
# Clone the repo
git clone https://github.com/CraftsMan-Labs/SimpleCode.git
cd SimpleCode

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

Requires **Python 3.13+** and at least one agent CLI on your PATH:
- `claude` (Claude Code)
- `agent` (Cursor CLI)
- `codex` (OpenAI Codex)
- `opencode` (OpenCode)

## Create your first pipeline

```bash
# From the repo you want agents to work in
uv run simplecode init feature-dev --agents 3
```

This creates a **project** pipeline under `.simplecode/`:

```
.simplecode/
└── pipelines/
    └── feature-dev/
        ├── pipeline.yaml          # pipeline definition
        ├── setup.sh               # environment setup (optional)
        └── prompts/
            ├── planner/
            │   ├── 1.md            # first prompt
            │   └── 2.md            # second prompt
            ├── executor/
            │   ├── 1.md
            │   └── 2.md
            └── reviewer/
                ├── 1.md
                └── 2.md
```

Use `--global` to put a reusable pipeline in `~/.simplecode/pipelines/` instead. See [simplecode-dir.md](simplecode-dir.md).

## Edit your prompts

Prompts are **static templates** — they define what each agent *does* (e.g., "scan the codebase", "implement the plan"). Each numbered file runs in sequence within the same agent session.

**Example `prompts/planner/1.md`:**

```markdown
Scan this codebase. List:
- Project type and tech stack
- Entry points and API endpoints
- Key data models
```

**Example `prompts/planner/2.md`:**

```markdown
Based on your scan, create an implementation plan for the task described in the context.
Include file paths, order of changes, and risks.
```

## Write your task

The specific task you want accomplished goes in a separate file, passed via `-i`:

```bash
echo "Add OAuth2 login to the /api/auth endpoint using Google as the provider" > TASK.md
```

You can pass multiple input files — they're all injected as context into the first agent's first prompt:

```bash
-i TASK.md -i CONSTRAINTS.md -i API-SPEC.md
```

Each subsequent agent receives the previous agent's `output.md` as context automatically:

```
TASK.md ──→ planner (prompts 1.md, 2.md)
                │
                └─ output.md ──→ executor (prompt 1.md)
                                     │
                                     └─ output.md ──→ reviewer (prompt 1.md)
```

## Run the pipeline

```bash
uv run simplecode list
uv run simplecode run feature-dev \
  -r claude \
  -w . \
  -t 86400 \
  -i TASK.md
```

| Flag / arg | What it does |
|------|-------------|
| `PIPELINE` | Pipeline name from `.simplecode`, or a path |
| `-f` | Explicit path to `pipeline.yaml` (optional if you pass a name) |
| `-r` | Runtime: `claude`, `cursor`, `codex`, or `opencode` |
| `-w` | Working directory — the repo the agent operates on (also used to find project `.simplecode`) |
| `-t` | Timeout per agent in seconds (86400 = 24 hours) |
| `-i` | Input file(s) — your task description, injected as context to the first agent |
| `-m` | Default model (e.g. `opus`, `sonnet`) |

## What happens

1. **setup.sh** runs (if uncommented in pipeline.yaml)
2. The first agent receives your `-i` input files as context alongside its prompts
3. Each agent runs its numbered prompts in sequence (same CLI session)
4. At gates (`gate: human`), you review the output and give feedback
5. Type **`approve`** or **`PNS`** to advance to the next agent
6. Each agent's output passes as context to the next agent
7. All artifacts are saved to `./pipeline-output/{session-id}/`

## Next steps

- [Pipeline YAML Reference](pipeline-yaml.md) — all config options
- [Human Gates](gates.md) — how feedback loops work
- [Python API](python-api.md) — build pipelines in code
- [Runtimes](runtimes.md) — claude, cursor, codex, opencode details

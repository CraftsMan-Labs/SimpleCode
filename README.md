# SimpleCode

Multi-agent pipeline orchestrator for AI coding agents. Chain multiple agents with numbered prompts, human-in-the-loop gates, and context passing between agents.

Supported runtimes: `claude`, `cursor`, `codex`, `opencode`.

## Install

```bash
pip install git+https://github.com/CraftsMan-Labs/SimpleCode.git
```

Optional: AST code search via [cocoindex-code](https://github.com/cocoindex-io/cocoindex-code) (`simplecode sync` / `search` / `--code-context`):

```bash
pip install 'simplecode[code]'
```

Pin to a release:

```bash
pip install git+https://github.com/CraftsMan-Labs/SimpleCode.git@v0.1.0
```

## Quick start

```bash
simplecode init feature-dev
echo "Add OAuth2 login to the API" > TASK.md
simplecode run feature-dev          # picks up TASK.md (or ISSUE.md) automatically
# or: simplecode run feature-dev TASK.md
# or: simplecode run feature-dev "Add OAuth2 login to the API"
```

Pipelines: `.simplecode/pipelines/` or `~/.simplecode/pipelines/`. See [docs/simplecode-dir.md](docs/simplecode-dir.md).

## Loading a task

Prompts (`1.md`, `2.md`, ...) are **static templates** that define what each agent does. The specific task you want to accomplish is passed via `-i` (input files):

```bash
simplecode run -f pipeline.yaml -r claude -w /path/to/repo \
  -i TASK.md \
  -i CONSTRAINTS.md \
  -t 86400
```

Input files are injected as context into the **first agent's first prompt**. Each subsequent agent automatically receives the previous agent's output as context instead.

```
TASK.md ──→ planner (1.md, 2.md)
                │
                └─ output.md ──→ executor (1.md)
                                     │
                                     └─ output.md ──→ reviewer (1.md)
```

## Pipeline YAML

```yaml
name: feature-dev
description: Plan, execute, and review

# setup: setup.sh   # optional — runs before agents start

agents:
  - id: planner
    prompts: prompts/planner/
    gate: human
    permissions: read-only
    timeout: 86400
    model: opus

  - id: executor
    prompts: prompts/executor/
    runtime: opencode          # override the default runtime
    permissions: write
    timeout: 86400

  - id: reviewer
    prompts: prompts/reviewer/
    gate: human
    permissions: read-only
```

### Agent fields

| Field | Required | Default | Values |
|-------|----------|---------|--------|
| `id` | yes | — | unique string |
| `prompts` | yes | — | path to dir with `1.md`, `2.md`, ... |
| `gate` | no | none | `human` |
| `permissions` | no | `write` | `read-only`, `write`, `full` |
| `timeout` | no | `300` | seconds |
| `runtime` | no | CLI default | `claude`, `cursor`, `codex`, `opencode` |
| `model` | no | CLI default | any model the runtime supports |

## CLI

```bash
simplecode run -f pipeline.yaml [options]
```

| Flag | Default | Description |
|------|---------|-------------|
| `-r, --runtime` | `claude` | Default runtime for all agents |
| `-w, --working-dir` | `.` | Repo the agents work in |
| `-t, --timeout` | `300` | Default timeout per agent (seconds) |
| `-m, --model` | — | Default model |
| `-i, --input` | — | Task/context files injected into the first agent (repeatable) |
| `-o, --output` | `./pipeline-output` | Output directory |
| `--permissions` | `write` | Default permission level |
| `--gate-mode` | `stdin` | `stdin` (interactive) or `file` (CI/cloud) |
| `--gate-dir` | — | Directory for file-based gate artifacts |

Other commands:

```bash
simplecode init <name> --agents <n>   # scaffold a new pipeline
simplecode list -f pipeline.yaml      # show agents and prompt counts
```

## Gates

Agents with `gate: human` pause the pipeline after running. You then enter a feedback loop in the same CLI session:

- **Type feedback** — sent to the agent, it responds, you review again
- **Type `PNS`** — proceeds to the next agent
- **Type `reject` / `quit` / `abort`** — stops the pipeline

### File-based gates (CI/cloud)

Use `--gate-mode file --gate-dir ./gates` for non-interactive environments.

The orchestrator writes `{gate-dir}/{agent-id}.gate` with the agent's output. To respond:

- Write feedback to `{agent-id}.feedback` — agent processes it, updates `.gate`
- Write `PNS` to `{agent-id}.feedback` — advances the pipeline
- Create `{agent-id}.rejected` — stops the pipeline

## Python API

```python
from pathlib import Path
from simplecode import Pipeline, PipelineConfig, AgentConfig

config = PipelineConfig(
    name="my-pipeline",
    agents=[
        AgentConfig(id="planner", prompts="prompts/planner/", gate="human", permissions="read-only"),
        AgentConfig(id="executor", prompts="prompts/executor/", permissions="write"),
    ],
)

pipeline = Pipeline(config, pipeline_dir=Path("./my-pipeline"))
result = pipeline.run(
    runtime="claude",
    working_dir="/path/to/repo",
    timeout=86400,
)
```

## How it works

1. Load `pipeline.yaml`, validate config
2. Run `setup.sh` if defined
3. For each agent:
   - Run numbered prompts (`1.md`, `2.md`, ...) sequentially in one CLI session
   - If `gate: human` — enter feedback loop, advance on `PNS`
   - Write `output.md` — automatically passed as context to the next agent

Output goes to `pipeline-output/{session-id}/`:

```
pipeline-output/a1b2c3d4/
├── pipeline.json
├── planner/
│   ├── prompt-1.md
│   ├── 1-output.md
│   ├── prompt-2.md
│   ├── 2-output.md
│   ├── gate-1-input.md
│   ├── gate-1-output.md
│   └── output.md
└── executor/
    └── ...
```

## Development

```bash
git clone https://github.com/CraftsMan-Labs/SimpleCode.git
cd SimpleCode
make dev          # install with dev deps
make check        # lint + typecheck + tests
make fmt          # auto-format
make test-live    # run live tests (needs a runtime on PATH)
```

Requires Python 3.13+.

## Docs

See the [docs/](docs/) directory for detailed guides:

- [Getting started](docs/getting-started.md)
- [Pipeline YAML reference](docs/pipeline-yaml.md)
- [CLI reference](docs/cli-reference.md)
- [Gates](docs/gates.md)
- [Runtimes](docs/runtimes.md)
- [Python API](docs/python-api.md)
- [Examples](docs/examples.md)
- [Output artifacts](docs/output-artifacts.md)

## License

Proprietary — CraftsmanLabs

# Python API

You can create and run pipelines entirely from Python — no YAML needed.

## Quick start

```python
from pathlib import Path
from simplecode import Pipeline, PipelineConfig, AgentConfig

config = PipelineConfig(
    name="my-pipeline",
    description="Plan and execute a feature",
    setup="setup.sh",  # optional
    agents=[
        AgentConfig(
            id="planner",
            prompts="prompts/planner/",
            gate="human",
            permissions="read-only",
            model="opus",
            timeout=86400,
        ),
        AgentConfig(
            id="executor",
            prompts="prompts/executor/",
            permissions="write",
            model="sonnet",
            timeout=86400,
        ),
    ],
)

pipeline = Pipeline(config, pipeline_dir=Path("./my-pipeline"))
result = pipeline.run(
    runtime="claude",
    working_dir="/path/to/target/repo",
    input_files=["TASK.md"],
    gate_mode="stdin",
    output_dir="./pipeline-output",
    timeout=86400,
)

print(f"Completed: {result.agents_completed}/{result.total_agents}")
print(f"Output: {result.output_dir}")
```

## Load from YAML

```python
from pathlib import Path
from simplecode import Pipeline, PipelineConfig

config = PipelineConfig.load(Path("./my-pipeline/pipeline.yaml"))
pipeline = Pipeline(config, pipeline_dir=Path("./my-pipeline"))

result = pipeline.run(
    runtime="claude",
    working_dir="/path/to/repo",
)
```

## Run a single agent

```python
from pathlib import Path
from simplecode import AgentRunner
from simplecode.runtime import RUNTIMES

runner = AgentRunner(
    agent_id="reviewer",
    runtime_config=RUNTIMES["claude"],
    output_dir=Path("./output"),
    timeout=86400,
    session_id="my-session-01",
    permissions="read-only",
    model="opus",
)

result = runner.run(
    prompts_dir=Path("./prompts/reviewer/"),
    context_files=["./prior-output.md"],
    working_dir="/path/to/repo",
)

print(f"Prompts run: {result.prompt_count}")
print(f"Output: {result.output_dir / 'output.md'}")
```

## AgentConfig fields

```python
AgentConfig(
    id="planner",              # unique agent ID (required)
    prompts="prompts/planner/", # path to numbered prompts dir (required)
    gate="human",               # "human" or None (default: None)
    permissions="read-only",    # "read-only", "write", "full" (default: "write")
    model="opus",               # model override (default: None, uses pipeline default)
    timeout=86400,              # seconds (default: 300)
    runtime="claude",           # runtime override (default: None, uses pipeline default)
)
```

## PipelineResult fields

```python
result.session_id        # str — unique ID for this run
result.agents_completed  # int — how many agents finished
result.total_agents      # int — total agents in pipeline
result.gate_rejected_at  # str | None — agent ID where human rejected
result.output_dir        # Path — where all artifacts live
result.status            # str — complete, checkpointed, or rejected
result.checkpoint_agent  # str | None — gated agent when status is checkpointed
```

## AgentResult fields

```python
result.agent_id      # str — agent ID
result.session_id    # str — pipeline session ID
result.output_dir    # Path — this agent's output directory
result.prompt_count  # int — how many prompts were run
result.success       # bool
result.final_output  # str — text of last agent response
```

## Error handling

```python
from simplecode.errors import (
    SimpleCodeError,       # base class — catch all simplecode errors
    PipelineConfigError,   # bad YAML, missing prompts, validation failures
    SetupError,            # setup.sh failed
    AgentError,            # agent CLI failed or timed out
    GateRejectedError,     # human rejected at gate
    RuntimeNotFoundError,  # CLI binary not on PATH
)

try:
    result = pipeline.run(runtime="claude", working_dir="/repo")
except SetupError as e:
    print(f"Setup failed: {e.script} (exit {e.exit_code})")
except RuntimeNotFoundError as e:
    print(f"Install {e.binary} first")
except AgentError as e:
    print(f"Agent failed: {e.runtime} (exit {e.exit_code})")
except PipelineConfigError as e:
    print(f"Config error: {e}")
```

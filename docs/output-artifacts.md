# Output Artifacts

Every pipeline run creates a session directory with all artifacts.

## Directory structure

```
pipeline-output/{session-id}/
├── pipeline.json                 # pipeline metadata and status
├── planner/
│   ├── prompt-1.md               # raw prompt text sent for prompt 1
│   ├── prompt-2.md               # raw prompt text sent for prompt 2
│   ├── prompt-3.md
│   ├── 1-output.md               # agent response to prompt 1
│   ├── 2-output.md               # agent response to prompt 2
│   ├── 3-output.md               # agent response to prompt 3
│   ├── gate-1-input.md           # human feedback round 1 (if gated)
│   ├── gate-1-output.md          # agent response to feedback 1
│   ├── gate-2-input.md           # human feedback round 2
│   ├── gate-2-output.md          # agent response to feedback 2
│   └── output.md                 # final output → passed to next agent
├── executor/
│   ├── prompt-1.md
│   ├── 1-output.md
│   ├── prompt-2.md
│   ├── 2-output.md
│   └── output.md
└── ...
```

## pipeline.json

Top-level metadata for the run:

```json
{
  "pipeline": "feature-dev",
  "description": "Plan, build, review, and fix a feature",
  "session_id": "a1b2c3d4",
  "runtime": "claude",
  "model": "opus",
  "agents_completed": 5,
  "total_agents": 5,
  "gate_rejected_at": null,
  "status": "complete",
  "timestamp": "2025-06-14T12:00:00+00:00",
  "branch": "simplecode/a1b2c3d4/20260913-094758-2da362",
  "branch_strategy": "merge-to-head",
  "worktree_path": null,
  "docker_image": null,
  "merge_result": "merged",
  "preserved_worktree": false,
  "agents": [
    {"id": "planner", "prompts": "prompts/planner/", "gate": "human", ...},
    {"id": "executor", "prompts": "prompts/executor/", "gate": null, ...}
  ]
}
```

When workspace isolation is on, `branch`, `branch_strategy`, `worktree_path`, `docker_image`, `merge_result`, and `preserved_worktree` tell you what happened to the git tree. See [Workspace isolation](workspace.md).

## output.md

Each agent's `output.md` is a structured file with metadata:

```markdown
# Agent: planner

- **Session:** a1b2c3d4
- **Runtime:** claude
- **Model:** opus
- **Timestamp:** 2025-06-14T12:00:00+00:00

---

## Output

[agent's final response text]
```

This is the file that gets passed as context to the next agent.

## Context chain

```
-i TASK.md ──→ planner/output.md ──→ executor/output.md ──→ reviewer/output.md
               (context for          (context for            (context for
                executor)             reviewer)               review-tasks)
```

Each agent's first prompt receives all prior agents' `output.md` files as inline context under a `## Context from previous agents` section.

# Plan → Execute → Review Pipeline

A 3-agent pipeline that plans a task, implements it, and reviews the result from security, implementation, and code cleanliness perspectives.

## Flow

```
TASK.md ──→ planner (scan → create PLAN.md → summarise)
                │
                └─ [GATE: human reviews plan, gives feedback or approves]
                        │
                        └──→ executor (implement plan → verify completion)
                                  │
                                  └──→ reviewer (security → implementation → cleanliness → verdict)
                                            │
                                            └─ [GATE: human reviews findings]
```

## Usage

1. Write your task:

```bash
cp TASK.md ~/projects/myapp/TASK.md
# Edit TASK.md with your actual task description
```

2. Run the pipeline:

```bash
simplecode run \
  -f pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  -i ~/projects/myapp/TASK.md \
  -t 86400
```

3. At the **planner gate**, review the plan summary:
   - Type feedback to refine the plan (stays in the same session)
   - Type `PNS` to approve and start execution

4. The executor implements the plan and verifies completion.

5. At the **reviewer gate**, review the security/implementation/cleanliness findings:
   - Type feedback if you want the reviewer to look deeper at something
   - Type `PNS` to finish

## Customisation

- **Add a model**: set `model: opus` on the planner/reviewer for deeper analysis, `model: sonnet` on the executor for faster implementation
- **Add a runtime**: set `runtime: opencode` on specific agents to mix runtimes
- **Add setup**: uncomment `setup: setup.sh` in pipeline.yaml and add your dependency installs
- **Edit prompts**: tweak any prompt in `prompts/` to match your workflow

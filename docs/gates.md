# Human Gates

Gates pause the pipeline after an agent finishes and let you review its work, give feedback, and only proceed when you're ready.

## How it works

```
Agent runs all its prompts (1.md → 2.md → 3.md)
  ↓
GATE ACTIVATES — shows you the agent's output
  ↓
You type feedback → sent to the SAME agent session
  ↓
Agent responds → you see the response
  ↓
You can keep giving feedback (same session)
  ↓
You type "approve" or "PNS" → pipeline moves to the next agent
```

The key: your feedback goes to the **same CLI session**. The agent has full context from its prompts and your previous feedback — it's a conversation, not a restart.

## Adding a gate

In your `pipeline.yaml`, add `gate: human` to any agent:

```yaml
agents:
  - id: planner
    prompts: prompts/planner/
    gate: human            # ← this agent gets a gate
    permissions: read-only

  - id: executor
    prompts: prompts/executor/
    permissions: write      # no gate — runs automatically after planner
```

## Gate commands

When a gate activates, you'll see this prompt:

```
============================================================
  GATE: Agent 'planner' completed
============================================================

[agent's output here]

============================================================
  Type feedback to send to the agent (same session).
  Type 'approve' or 'PNS' to proceed to the next agent.
  Type 'stop', 'reject', 'quit', or 'abort' to stop the pipeline.
============================================================

[gate] >
```

| You type | What happens |
|----------|-------------|
| `approve` or `PNS` | Pipeline proceeds to the next agent |
| `stop`, `reject`, `quit`, or `abort` | Pipeline stops |
| Anything else | Sent as feedback to the agent (same session) |
| Ctrl-C or EOF | Pipeline stops |

## Example conversation

```
[gate] > The plan looks good but you missed the authentication endpoint.
         Please add test cases for /api/auth/login and /api/auth/refresh.

  Sending feedback to agent 'planner'...

[agent responds with updated plan including auth endpoints]

[gate] > PNS
```

## File-based gates (for cloud/CI)

For headless environments where there's no terminal, use file-based gates:

```bash
uv run simplecode run \
  -f pipeline.yaml \
  -r claude \
  -w /path/to/repo \
  --gate-mode file \
  --gate-dir ./gates
```

### How file-based gates work

When a gate activates:

1. The orchestrator writes the agent's output to `gates/{agent-id}.gate`
2. It polls every 5 seconds for one of these files:

| File | Effect |
|------|--------|
| `gates/{agent-id}.feedback` | Content is sent as feedback to the agent. The file is deleted after reading. The agent's response replaces the `.gate` file. |
| `gates/{agent-id}.rejected` | Pipeline stops. |

3. To advance, write `PNS` to the feedback file:

```bash
echo "PNS" > gates/planner.feedback
```

4. To give feedback:

```bash
echo "Add more test cases for error handling" > gates/planner.feedback
```

5. To reject:

```bash
touch gates/planner.rejected
```

This lets you build a web UI, Slack bot, or any external system that interacts with the gate files.

## Checkpoint mode (async human gates)

For long-lived reviews where you can't keep a terminal open (e.g., reviewing a plan posted on GitHub), use checkpoint mode. The pipeline saves its state to a database and exits. You resume it later with `simplecode resume`.

### Setup

By default, checkpoint mode uses SQLite at `~/.simplecode/simplecode.db` — no setup needed.

For PostgreSQL (recommended for teams):

```bash
export SIMPLECODE_DATABASE_URL=postgres://localhost/simplecode
# or pass --database-url on the CLI
```

If PostgreSQL is configured but unreachable, the CLI will prompt you to fall back to SQLite.

### Running with checkpoints

```bash
simplecode run \
  -f pipeline.yaml \
  -r claude \
  -w /path/to/repo \
  --gate-mode checkpoint \
  -t 86400
```

When a gate activates, the pipeline:
1. Saves the full session state (agent outputs, context chain, config) to the database
2. Prints the session ID and resume instructions
3. Exits cleanly

### Resuming

```bash
# List waiting sessions
simplecode sessions --status waiting

# Approve the gate and continue
simplecode resume -s <session-id> --approve

# Send feedback to the gated agent (stays in the same CLI session)
simplecode resume -s <session-id> --feedback "Add error handling for edge case X"

# Reject and stop the pipeline
simplecode resume -s <session-id> --reject

# Drop into interactive mode (stdin gate)
simplecode resume -s <session-id>
```

### Multi-round feedback

Each `--feedback` call sends your feedback to the agent, gets a response, and re-checkpoints:

```bash
simplecode resume -s abc123 --feedback "Add tests for the auth endpoint"
# → Agent responds, pipeline re-checkpoints

simplecode resume -s abc123 --feedback "Also handle the 401 case"
# → Agent responds, pipeline re-checkpoints

simplecode resume -s abc123 --approve
# → Pipeline continues with the next agent
```

### Integration patterns

**GitHub Actions:** A workflow triggered by issue comments can call `simplecode resume` when a maintainer approves the plan.

**Cron:** Poll `simplecode sessions --status waiting` and notify a Slack channel.

**Manual:** Check sessions any time and resume from any machine with DB access.

### Session statuses

| Status | Meaning |
|--------|---------|
| `running` | Pipeline is actively executing |
| `waiting` | Checkpointed at a gate, awaiting human input |
| `resumed` | Was waiting, now running again after resume |
| `completed` | Pipeline finished successfully |
| `rejected` | Human rejected at a gate |

## Output artifacts

Gate interactions are saved as artifacts:

```
pipeline-output/{session}/planner/
├── 1-output.md              # agent prompt outputs
├── 2-output.md
├── gate-1-input.md          # your first feedback
├── gate-1-output.md         # agent's response to feedback
├── gate-2-input.md          # your second feedback
├── gate-2-output.md         # agent's response
└── output.md                # final output (passed to next agent)
```

# Runtimes

SimpleCode supports four AI agent CLI backends. Use the `-r` flag to choose one.

## Supported runtimes

| Runtime | Binary | Install |
|---------|--------|---------|
| `claude` | `claude` | [Claude Code CLI](https://claude.ai/code) |
| `cursor` | `agent` | `curl -fsSL https://cursor.com/install \| bash` |
| `codex` | `codex` | `npm install -g @openai/codex` |
| `opencode` | `opencode` | [opencode.ai](https://opencode.ai) |

## How runtimes are used

Each agent runs its prompts as CLI subprocess calls:

- **Prompt 1** starts a new session
- **Prompts 2+** resume the same session
- **Gate feedback** also resumes the same session

The runtime controls exactly which CLI flags are used for new/resume calls.

## Permission levels

Each runtime maps the three permission levels differently:

### claude

| Level | Flags |
|-------|-------|
| `read-only` | `--permission-mode plan` |
| `write` | `--permission-mode acceptEdits` |
| `full` | `--permission-mode bypassPermissions` |

### cursor

| Level | Flags |
|-------|-------|
| `read-only` | `--mode ask` |
| `write` | `--force --trust` |
| `full` | `--force --trust --approve-mcps` |

### codex

| Level | Flags |
|-------|-------|
| `read-only` | `--sandbox read-only --ask-for-approval never` |
| `write` | `--sandbox workspace-write --ask-for-approval never` |
| `full` | `--sandbox danger-full-access --ask-for-approval never` |

### opencode

| Level | Flags |
|-------|-------|
| `read-only` | *(none)* |
| `write` | `--dangerously-skip-permissions` |
| `full` | `--dangerously-skip-permissions` |

## Per-agent runtime override

You can mix runtimes within a single pipeline:

```yaml
agents:
  - id: planner
    prompts: prompts/planner/
    runtime: claude           # use claude for planning
    model: opus

  - id: executor
    prompts: prompts/executor/
    runtime: codex            # use codex for execution
    model: gpt-5.4
```

## Model selection

Models are passed via the runtime's model flag:

| Runtime | Flag | Example values |
|---------|------|----------------|
| `claude` | `--model` | `opus`, `sonnet`, `haiku`, `claude-opus-4-6` |
| `cursor` | `--model` | `sonnet-4`, `opus-4` |
| `codex` | `-m` | `gpt-5.4`, `o3` |
| `opencode` | `-m` | `provider/model` (e.g. `anthropic/claude-sonnet-4-6`) |

Set a default with `-m` on the CLI, or override per-agent in YAML with `model:`.

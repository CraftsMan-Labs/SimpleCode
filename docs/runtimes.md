# Runtimes

SimpleCode supports five AI agent CLI backends. Use the `-r` flag to choose one.

## Supported runtimes

| Runtime | Binary | Install |
|---------|--------|---------|
| `claude` | `claude` | [Claude Code CLI](https://claude.ai/code) |
| `cursor` | `agent` | `curl -fsSL https://cursor.com/install \| bash` |
| `codex` | `codex` | `npm install -g @openai/codex` |
| `opencode` | `opencode` | [opencode.ai](https://opencode.ai) |
| `pi` | `pi` | `npm install -g --ignore-scripts @earendil-works/pi-coding-agent` |

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

### pi

| Level | Flags |
|-------|-------|
| `read-only` | `--tools read,grep,find,ls` |
| `write` | `--tools read,bash,edit,write,grep,find,ls` |
| `full` | `--tools read,bash,edit,write,grep,find,ls` |

Pi has no native sandbox or permission-prompt flag, so `write` and `full` expose the
same Pi tool set. Use SimpleCode workspace isolation or Docker when you need host-level
write isolation.

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
| `pi` | `--model` | `provider/model` (e.g. `openai/gpt-5.4`) |

Set a default with `-m` on the CLI, or override per-agent in YAML with `model:`.

## Streaming and idle timeouts

Agent output streams live. Per-agent `timeout` is an idle silence window: any stdout/stderr chunk resets the deadline. Quiet hangs get terminate-then-kill after that window. Agents that keep printing can run longer than the timeout value; that is intentional.

## Docker execution

With `docker_image` / `--docker-image`, SimpleCode starts one container for the run, bind-mounts the effective worktree at `/workspace`, and prefixes agent commands with `docker exec -i -w /workspace <id>`.

Your image needs the runtime binary (`claude`, `agent`, `codex`, `opencode`, or `pi`) on PATH. The container goes away when the pipeline exits (including checkpoints); resume opens a new one on the same worktree. More detail: [Workspace isolation](workspace.md).

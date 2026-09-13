# CocoIndex Code (optional)

AST semantic search for the repo agents work in. SimpleCode shells out to [`ccc`](https://github.com/cocoindex-io/cocoindex-code) — it does not vendor cocoindex-code.

## Install

```bash
pip install 'simplecode[code]'
# or: uv sync --extra code
```

Or run `ccc` through the shared Docker stack (no host Python install) — see [Shared agent stack](agent-stack.md) and `./docker/ccc`.

First time in a target repo:

```bash
cd /path/to/repo
ccc init    # once — pick embedding model
simplecode sync -w .
```

## CLI

```bash
simplecode sync -w .                          # ccc index
simplecode search "auth middleware" -w .      # ccc search

# Inject search hits into the first agent
simplecode run feature-dev TASK.md \
  --code-context "OAuth login flow" \
  --code-refresh
```

## Agents / MCP (separate from SimpleCode)

Coding agents can also talk to cocoindex directly:

```bash
claude mcp add cocoindex-code -- ccc mcp
# or: npx skills add cocoindex-io/cocoindex-code
```

SimpleCode does not need to wrap MCP — install once, agents use `search` themselves while pipelines use `--code-context` / `sync` when you want pack injection.

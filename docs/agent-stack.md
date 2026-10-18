# Shared agent stack (9Router, CocoIndex)

One Docker Compose project for your whole machine. Start it once; use it from every repo. It is separate from SimpleCode's per-run `docker_image` sandbox — this stack is the long-lived sidecars.

| Service | Container | Host URL | Role |
|---------|-----------|----------|------|
| [9Router](https://github.com/decolua/9router) | `simplecode-9router` | `http://127.0.0.1:20128` | OpenAI-compatible gateway, fallback, RTK |
| [CocoIndex Code](https://github.com/cocoindex-io/cocoindex-code) | `simplecode-cocoindex` | (CLI / MCP via `docker exec`) | AST index + search for any project under `$HOME` |

## Start / stop

```bash
cd /path/to/SimpleCode
docker compose -f docker/compose.yml up -d

# Linux: match host UID/GID for files cocoindex writes
PUID=$(id -u) PGID=$(id -g) docker compose -f docker/compose.yml up -d

docker compose -f docker/compose.yml down
```

Makefile shortcuts: `make stack-up` / `make stack-down` / `make stack-ps`.

Optional knobs live in `docker/.env.example`. Copy to `docker/.env` if you want them.

### Narrow cocoindex's mount

By default cocoindex sees your entire `$HOME` (so every project works). To only index under a projects tree:

```bash
COCOINDEX_HOST_WORKSPACE=$HOME/Desktop/projects \
  docker compose -f docker/compose.yml up -d
```

### Local embeddings

```bash
COCOINDEX_CODE_IMAGE=cocoindex/cocoindex-code:full \
  docker compose -f docker/compose.yml up -d
```

## Wire coding agents on the host

1. Open the 9Router dashboard: http://127.0.0.1:20128 — default password is `123456` (override with `INITIAL_PASSWORD`). Docker published ports look remote to 9Router even on loopback; the compose file sets `INITIAL_PASSWORD` so the dashboard accepts that first login from the host.
2. Optional SSO: with Keycloak up (`make plane-sso-up`), run `make stack-sso`. The login page then has **Sign in with SSO** (password login stays available). Same Keycloak / LDAP users as Plane.
3. Add providers / API keys.
4. Point Claude Code / Codex / Cursor / etc. at `http://127.0.0.1:20128/v1` (and the dashboard API key if you enabled one).

## CocoIndex from any project

```bash
# one-shot
./docker/ccc init
./docker/ccc index
./docker/ccc search "auth middleware"

# or alias
alias ccc='$HOME/.../SimpleCode/docker/ccc'
```

MCP (Claude / Codex) from a project directory:

```bash
claude mcp add cocoindex-code -- docker exec -i \
  -e COCOINDEX_CODE_HOST_CWD="$PWD" simplecode-cocoindex ccc mcp
```

SimpleCode's own `simplecode sync` / `search` still expect a host `ccc` on `PATH`. Either install `cocoindex-code` on the host, or put the `docker/ccc` wrapper on `PATH` as `ccc`.

## Optional agent sandbox image

For SimpleCode `docker_image` runs (bind-mount worktree at `/workspace`):

```bash
docker build -t simplecode-agent:latest \
  -f docker/agent/Dockerfile docker/agent

uv run simplecode run feature-dev \
  --docker-image simplecode-agent:latest \
  -w ~/projects/myapp
```

When network `simplecode-stack` exists, SimpleCode joins it automatically so the agent container can reach `http://9router:20128`. Override with `SIMPLECODE_DOCKER_NETWORK=other-net`, or set it empty to skip.

Default env in the image:

- `ANTHROPIC_BASE_URL=http://9router:20128`
- `OPENAI_BASE_URL=http://9router:20128/v1`

Install extra agent CLIs (Cursor `agent`, Codex, OpenCode, Pi) in a derived Dockerfile if you need them.

## What this is not

- Not a replacement for `branch_strategy` / worktrees — see [Workspace isolation](workspace.md).
- Not per-project compose files. One stack, many repos.
- Provider keys live in 9Router's data dir (`~/.9router` by default), not in SimpleCode YAML.

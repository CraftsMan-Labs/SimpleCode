# Optional long-lived agent sandbox for SimpleCode `docker_image`.
# Build once, reuse from any project:
#   docker build -t simplecode-agent:latest -f docker/agent/Dockerfile docker/agent
#
# Requires the shared stack network when you want in-container API access:
#   docker compose -f docker/compose.yml up -d
# SimpleCode auto-joins network `simplecode-stack` when it exists.
#
# Point agent CLIs at 9Router (which forwards through Headroom):
#   ANTHROPIC_BASE_URL=http://9router:20128
#   OPENAI_BASE_URL=http://9router:20128/v1
# Or from the host (no agent image): http://127.0.0.1:20128

FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    jq \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
  && apt-get update && apt-get install -y --no-install-recommends gh \
  && rm -rf /var/lib/apt/lists/*

ARG AGENT_UID=1000
ARG AGENT_GID=1000

RUN groupmod -o -g "$AGENT_GID" node \
  && usermod -o -u "$AGENT_UID" -g "$AGENT_GID" -d /home/agent -m -l agent node

USER ${AGENT_UID}:${AGENT_GID}

RUN curl -fsSL https://claude.ai/install.sh | bash

ENV PATH="/home/agent/.local/bin:$PATH" \
    ANTHROPIC_BASE_URL=http://9router:20128 \
    OPENAI_BASE_URL=http://9router:20128/v1 \
    OPENAI_API_BASE=http://9router:20128/v1

WORKDIR /workspace

# SimpleCode starts the container with `sleep infinity`.
CMD ["sleep", "infinity"]

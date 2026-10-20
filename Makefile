.PHONY: install dev test test-live lint typecheck fmt check clean build docs \
	stack-up stack-down stack-ps stack-logs agent-image \
	plane-init plane-up plane-down plane-ps plane-logs \
	plane-sso-init plane-sso-up plane-sso-down plane-sso-logs plane-sso-user \
	stack-sso coder-init coder-up coder-down coder-logs coder-template-push \
	coder-sso-config coder-sso-up

STACK_COMPOSE := docker/compose.yml

install:
	uv sync

dev:
	uv sync --extra dev

test:
	uv run pytest tests/ -m "not live" -v

test-live:
	uv run pytest tests/ -m live -v

test-all:
	uv run pytest tests/ -v

lint:
	uv run ruff check src/ tests/

typecheck:
	uv run mypy src/

fmt:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

check: lint typecheck test

clean:
	rm -rf dist/ build/ .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name '*.egg-info' -exec rm -rf {} +

build: clean
	uv build

init:
	uv run simplecode init $(name) --agents $(or $(agents),5)

# One-command local coding environment: model/index services, the agent image,
# Plane + Keycloak, and the Keycloak-authenticated Coder control plane.
stack-up: agent-image coder-init plane-sso-init
	docker compose -f $(STACK_COMPOSE) up -d
	docker compose $(PLANE_SSO) up -d
	./docker/plane/sso/configure-plane.sh
	./docker/plane/sso/configure-9router.sh
	./docker/coder/configure-keycloak.sh
	$(CODER_SSO_COMPOSE) up -d

# Shared networks are owned by the core stack, so consumers stop first.
stack-down:
	@if test -f docker/coder/.env; then $(CODER_SSO_COMPOSE) down; fi
	@if test -f docker/plane/.env; then docker compose $(PLANE_SSO) down; fi
	docker compose -f $(STACK_COMPOSE) down

stack-ps:
	docker compose -f $(STACK_COMPOSE) ps
	@if test -f docker/plane/.env; then docker compose $(PLANE_SSO) ps; fi
	@if test -f docker/coder/.env; then $(CODER_SSO_COMPOSE) ps; fi

stack-logs:
	docker compose -f $(STACK_COMPOSE) logs -f --tail=100

agent-image:
	docker build -t simplecode-agent:latest -f docker/agent/Dockerfile docker/agent

PLANE_COMPOSE := docker/plane/compose.yml

plane-init:
	@test -f docker/plane/.env || { \
	  cp docker/plane/.env.example docker/plane/.env; \
	  sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$$(openssl rand -hex 32)/" docker/plane/.env; \
	  sed -i "s/^LIVE_SERVER_SECRET_KEY=.*/LIVE_SERVER_SECRET_KEY=$$(openssl rand -hex 32)/" docker/plane/.env; \
	  echo "wrote docker/plane/.env with fresh secrets"; }

plane-up: plane-init
	docker compose -f $(PLANE_COMPOSE) up -d

plane-down:
	docker compose -f $(PLANE_COMPOSE) down

plane-ps:
	docker compose -f $(PLANE_COMPOSE) ps

plane-logs:
	docker compose -f $(PLANE_COMPOSE) logs -f --tail=100

PLANE_SSO := -f $(PLANE_COMPOSE) -f docker/plane/sso/compose.yml

# Append SSO secrets to docker/plane/.env (once).
plane-sso-init: plane-init
	@grep -q '^KEYCLOAK_CLIENT_SECRET=' docker/plane/.env || { \
	  printf '\n# --- SSO layer (make plane-sso-init) ---\nLDAP_ADMIN_PASSWORD=%s\nKEYCLOAK_ADMIN_PASSWORD=%s\nKEYCLOAK_CLIENT_SECRET=%s\n' \
	    "$$(openssl rand -hex 16)" "$$(openssl rand -hex 16)" "$$(openssl rand -hex 24)" >> docker/plane/.env; \
	  echo "appended SSO secrets to docker/plane/.env"; }

# LDAP + Keycloak + shim, then wire Plane's Gitea login to it.
# Also points 9Router at the same Keycloak if the agent stack is up.
plane-sso-up: plane-sso-init
	docker compose $(PLANE_SSO) up -d
	./docker/plane/sso/configure-plane.sh
	-./docker/plane/sso/configure-9router.sh

# 9Router dashboard OIDC -> existing Keycloak (requires stack-up + plane-sso-up).
stack-sso: plane-sso-init
	./docker/plane/sso/configure-9router.sh

plane-sso-down:
	docker compose $(PLANE_SSO) down

plane-sso-logs:
	docker compose $(PLANE_SSO) logs -f --tail=100 openldap keycloak sso-shim sso-edge

# Seed a Keycloak login at runtime. Never writes the password to git, LDAP LDIF, or Plane's DB.
#   make plane-sso-user EMAIL=you@example.com PASSWORD='...'
# Optional: USERNAME= FIRST= LAST=
plane-sso-user:
	@test -n "$(EMAIL)" && test -n "$(PASSWORD)" || { \
	  echo "usage: make plane-sso-user EMAIL=you@example.com PASSWORD='...' [USERNAME=] [FIRST=] [LAST=]"; \
	  exit 1; }
	EMAIL="$(EMAIL)" PASSWORD="$(PASSWORD)" USERNAME="$(USERNAME)" FIRST="$(FIRST)" LAST="$(LAST)" \
	  ./docker/plane/sso/seed-user.sh

CODER_COMPOSE := docker compose --env-file docker/coder/.env -f docker/coder/compose.yml
CODER_SSO_COMPOSE := $(CODER_COMPOSE) -f docker/coder/compose.sso.yml

# Local-only Coder control plane. The Docker socket is intentionally limited to
# this development bundle and is not a production deployment pattern.
coder-init:
	@./docker/coder/init.sh

# Keep the public convenience target on the same Keycloak path as stack-up.
coder-up: coder-sso-up

coder-down:
	@if test -f docker/coder/.env; then $(CODER_COMPOSE) down; fi

coder-logs: coder-init
	$(CODER_COMPOSE) logs -f --tail=100

# Publish/update the reference workspace template after signing in with the
# local Coder CLI (or by setting CODER_SESSION_TOKEN).
coder-template-push: coder-init
	CODER_URL="$$(sed -n 's/^CODER_ACCESS_URL=//p' docker/coder/.env)" \
	  coder templates push simplecode-factory --directory docker/coder/template --yes

# Create/update Coder's confidential client in the existing Plane Keycloak realm.
coder-sso-config: coder-init plane-sso-up
	./docker/coder/configure-keycloak.sh

coder-sso-up: coder-sso-config
	$(CODER_SSO_COMPOSE) up -d

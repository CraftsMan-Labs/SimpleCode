.PHONY: install dev test test-live lint typecheck fmt check clean build docs \
	stack-up stack-down stack-ps stack-logs agent-image \
	plane-init plane-up plane-down plane-ps plane-logs \
	plane-sso-init plane-sso-up plane-sso-down plane-sso-logs

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

stack-up:
	docker compose -f $(STACK_COMPOSE) up -d

stack-down:
	docker compose -f $(STACK_COMPOSE) down

stack-ps:
	docker compose -f $(STACK_COMPOSE) ps

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
plane-sso-up: plane-sso-init
	docker compose $(PLANE_SSO) up -d
	./docker/plane/sso/configure-plane.sh

plane-sso-down:
	docker compose $(PLANE_SSO) down

plane-sso-logs:
	docker compose $(PLANE_SSO) logs -f --tail=100 openldap keycloak sso-shim

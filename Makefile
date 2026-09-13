.PHONY: install dev test test-live lint typecheck fmt check clean build docs \
	stack-up stack-down stack-ps stack-logs agent-image

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

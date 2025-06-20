.PHONY: install dev test test-live lint typecheck fmt check clean build docs

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

.PHONY: setup check lint format types test run image-build

setup:
	uv sync --locked --dev

check: lint types test

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .

format:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .

types:
	uv run --locked mypy

test:
	uv run --locked pytest

run:
	uv run --locked control-plane

image-build:
	docker build -t foxtrot/control-plane:local .

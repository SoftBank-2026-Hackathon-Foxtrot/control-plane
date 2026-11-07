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

.PHONY: local-run local-test
local-run:
	uv run --locked python dev/run_local.py

local-test:
	CONTROL_PLANE_TEST_DYNAMODB_URL=http://127.0.0.1:18005 uv run --locked pytest tests/integration/test_local_platform.py -q

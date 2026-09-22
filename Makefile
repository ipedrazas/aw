# The short set. Taskfile.yml has the same targets and more (sessions, logs, debug
# serving); when a command changes in one, change it in the other.
.PHONY: install test lint fmt serve docker-build up down validate dry-run sessions

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff check --fix src tests
	uv run ruff format src tests

serve:
	uv run wf serve --reload

docker-build:
	docker build -t aw:local .

up:
	docker compose up --build

down:
	docker compose down

validate:
	uv run wf validate deep-research

dry-run:
	uv run wf run deep-research --case durable-execution

sessions:
	uv run wf sessions

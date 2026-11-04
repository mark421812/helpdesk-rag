IMAGE_NAME ?= helpdesk/ingestion-service

.PHONY: lock lock-check install lint format typecheck test test-unit test-integration config build-native build-amd64 build-arm64 network smoke up down migrate worker scheduler logs ps

lock:
	uv lock

lock-check:
	uv lock --check

install:
	uv sync --frozen --extra dev

lint:
	uv run --frozen ruff check .

format:
	uv run --frozen ruff format .

typecheck:
	uv run --frozen mypy

test:
	uv run --frozen pytest

test-unit:
	uv run --frozen pytest tests/unit -m "not integration"

test-integration:
	uv run --frozen pytest -m integration

config:
	docker compose config --quiet

build-native:
	docker buildx build --load -t $(IMAGE_NAME):local .

build-amd64:
	docker buildx build --platform linux/amd64 --load -t $(IMAGE_NAME):amd64 .

build-arm64:
	docker buildx build --platform linux/arm64 --load -t $(IMAGE_NAME):arm64 .

network:
	@network_name="$$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["networks"]["default"]["name"])')"; \
	docker network inspect "$$network_name" >/dev/null 2>&1 || docker network create "$$network_name"

smoke:
	python3 scripts/docker_smoke_test.py

up: network
	docker compose up --build

down:
	docker compose down

migrate:
	docker compose run --rm migrate

worker:
	uv run --frozen python -m ingestion_service.worker

scheduler:
	uv run --frozen python -m ingestion_service.scheduler

logs:
	docker compose logs --tail 200 -f ingestion-api ingestion-worker ingestion-scheduler

ps:
	docker compose ps -a

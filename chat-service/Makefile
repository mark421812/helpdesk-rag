IMAGE_NAME ?= helpdesk/chat-service

.PHONY: lock lock-check install lint format typecheck test test-unit test-contract test-security \
	test-integration config build-native build-amd64 build-arm64 network preflight migrate smoke \
	smoke-e2e up down logs ps

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

test-contract:
	uv run --frozen pytest tests/contract -m "not integration"

test-security:
	uv run --frozen pytest tests/security -m "not integration"

test-integration:
	uv run --frozen pytest tests/integration -m integration

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
	if ! docker network inspect "$$network_name" >/dev/null 2>&1; then \
		docker network create "$$network_name" >/dev/null; \
	fi

preflight:
	uv run --frozen python scripts/preflight.py

migrate:
	docker compose run --rm migrate

smoke:
	uv run --frozen python scripts/docker_smoke_test.py

smoke-e2e:
	uv run --frozen python scripts/chat_e2e_test.py

up: network
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs --tail=200 -f chat-api migrate

ps:
	docker compose ps -a

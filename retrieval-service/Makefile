IMAGE_NAME ?= helpdesk/retrieval-service

.PHONY: lock lock-check install lint format typecheck test test-unit test-contract test-integration config build-native build-amd64 build-arm64 keys-dev network smoke smoke-e2e up down logs ps

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

keys-dev:
	@mkdir -p secrets/dev
	@if [ -e secrets/dev/retrieval-private.pem ] || [ -e secrets/dev/retrieval-public.pem ]; then \
		echo "Development attestation keys already exist; refusing to overwrite."; \
	else \
		openssl genpkey -algorithm ED25519 -out secrets/dev/retrieval-private.pem; \
		openssl pkey -in secrets/dev/retrieval-private.pem -pubout -out secrets/dev/retrieval-public.pem; \
		chmod 600 secrets/dev/retrieval-private.pem; \
		chmod 644 secrets/dev/retrieval-public.pem; \
		echo "Created development Ed25519 key pair under secrets/dev/."; \
	fi

network:
	@network_name="$$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["networks"]["default"]["name"])')"; \
	docker network inspect "$$network_name" >/dev/null 2>&1 || docker network create "$$network_name"

smoke:
	python3 scripts/docker_smoke_test.py

smoke-e2e:
	SMOKE_REQUIRE_EVIDENCE=1 python3 scripts/docker_smoke_test.py

up: keys-dev network
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs --tail 200 -f retrieval-api

ps:
	docker compose ps -a

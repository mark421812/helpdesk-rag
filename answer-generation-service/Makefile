IMAGE_NAME ?= helpdesk/answer-generation-service

.PHONY: lock lock-check install lint format typecheck test test-unit test-contract test-security config build-native build-amd64 build-arm64 keys-dev network smoke smoke-live smoke-groq-live smoke-retrieval-e2e up down logs ps

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

config:
	docker compose config --quiet

build-native:
	docker buildx build --load -t $(IMAGE_NAME):local .

build-amd64:
	docker buildx build --platform linux/amd64 --load -t $(IMAGE_NAME):amd64 .

build-arm64:
	docker buildx build --platform linux/arm64 --load -t $(IMAGE_NAME):arm64 .

keys-dev:
	$(MAKE) -C ../retrieval-service keys-dev

network:
	@network_name="$$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["networks"]["default"]["name"])')"; \
	docker network inspect "$$network_name" >/dev/null 2>&1 || docker network create "$$network_name"

smoke:
	python3 scripts/docker_smoke_test.py

smoke-retrieval-e2e:
	python3 scripts/retrieval_answer_e2e.py

smoke-live:
	@if [ "$${RUN_LIVE_NVIDIA_SMOKE:-0}" != "1" ]; then \
		echo "Refusing live smoke: set RUN_LIVE_NVIDIA_SMOKE=1 and use synthetic data only."; exit 2; \
	fi
	python3 scripts/docker_smoke_test.py

smoke-groq-live:
	@if [ "$${RUN_LIVE_GROQ_SMOKE:-0}" != "1" ]; then \
		echo "Refusing live smoke: set RUN_LIVE_GROQ_SMOKE=1 and use synthetic/public data only."; exit 2; \
	fi
	python3 scripts/docker_smoke_test.py

up: keys-dev network
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs --tail 200 -f answer-generation-api

ps:
	docker compose ps -a

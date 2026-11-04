IMAGE_NAME ?= helpdesk/knowledge-index-service

.PHONY: lock lock-check install lint format typecheck test test-unit test-integration config build-native build-amd64 build-arm64 verify-cpu-amd64 verify-cpu-arm64 network smoke up down migrate worker

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

# 不指定 platform 時會使用 Docker 主機原生架構：Intel Mac 是 linux/amd64，
# Oracle ARM Linux 是 linux/arm64。
build-native:
	docker buildx build --load -t $(IMAGE_NAME):local .

build-amd64:
	docker buildx build --platform linux/amd64 --load -t $(IMAGE_NAME):amd64 .

build-arm64:
	docker buildx build --platform linux/arm64 --load -t $(IMAGE_NAME):arm64 .

verify-cpu-amd64: build-amd64
	docker run --rm --platform linux/amd64 $(IMAGE_NAME):amd64 python -c "import platform, torch; assert torch.version.cuda is None; print(platform.machine(), torch.__version__, 'CUDA=', torch.version.cuda)"

verify-cpu-arm64: build-arm64
	docker run --rm --platform linux/arm64 $(IMAGE_NAME):arm64 python -c "import platform, torch; assert torch.version.cuda is None; print(platform.machine(), torch.__version__, 'CUDA=', torch.version.cuda)"

smoke:
	python3 scripts/docker_smoke_test.py

network:
	@network_name="$$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["networks"]["default"]["name"])')"; \
	docker network inspect "$$network_name" >/dev/null 2>&1 || docker network create "$$network_name"

up: network
	docker compose up --build

down:
	docker compose down

migrate:
	docker compose run --rm migrate

worker:
	uv run --frozen python -m knowledge_index_service.worker

"""只讀 Docker 與程式 fingerprint 盤點。失敗只顯示型別，不 dump env/log。

不執行 compose up、HTTP POST、SQL、purge、API key probe 或模型下載。
Docker inspect 的環境資料只在記憶體內讀取，輸出明確白名單的非機密設定。
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[1]
SERVICES = {
    "knowledge-index-service": ("api", "worker"),
    "ingestion-service": ("ingestion-api", "ingestion-worker"),
    "retrieval-service": ("retrieval-api",),
    "answer-generation-service": ("answer-generation-api",),
    "chat-service": ("chat-api",),
}
SAFE_SETTINGS = {
    "LLM_PROVIDER",
    "LLM_PROVIDER_USAGE_MODE",
    "LLM_MODEL",
    "MODEL_ID",
    "MODEL_REVISION",
    "MAX_CONTEXT_TOKENS",
    "MAX_CHUNKS_PER_DOCUMENT",
    "DEFAULT_RETRIEVAL_LIMIT",
    "CANDIDATE_MULTIPLIER",
    "TORCH_NUM_THREADS",
    "EMBEDDING_MAX_CONCURRENCY",
    "APP_ENV",
}


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=25, check=False
    )
    if result.returncode:
        raise RuntimeError("Docker command unavailable")
    return result.stdout


def inventory() -> dict:
    info = json.loads(docker("info", "--format", "{{json .}}"))
    containers = []
    for project, services in SERVICES.items():
        for service in services:
            ids = docker(
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.service={service}",
            ).split()
            if len(ids) != 1:
                raise RuntimeError("Required service missing or ambiguous")
            data = json.loads(docker("inspect", ids[0]))[0]
            env = {}
            for entry in data["Config"].get("Env", []):
                key, _, value = entry.partition("=")
                if key in SAFE_SETTINGS:
                    if len(value) > 256 or any(ord(c) < 32 for c in value):
                        raise ValueError("Unsafe configuration value")
                    env[key] = value
            stats = json.loads(
                docker("stats", "--no-stream", "--format", "{{json .}}", ids[0])
            )
            containers.append(
                {
                    "project": project,
                    "service": service,
                    "image_id": data["Image"],
                    "settings": env,
                    "cpu_percent": stats["CPUPerc"],
                    "memory": stats["MemUsage"],
                }
            )
    hashes = {}
    for service in SERVICES:
        for path in sorted((WORKSPACE / service).glob("src/**/*.py")):
            hashes[path.relative_to(WORKSPACE).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        for name in ("pyproject.toml", "uv.lock", "Dockerfile", "compose.yaml"):
            path = WORKSPACE / service / name
            hashes[path.relative_to(WORKSPACE).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    policy = (
        WORKSPACE
        / "answer-generation-service/src/answer_generation_service/infrastructure"
        / "outbound_policy/policy.yaml"
    )
    hashes[policy.relative_to(WORKSPACE).as_posix()] = hashlib.sha256(
        policy.read_bytes()
    ).hexdigest()
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "stage": "read_only_inventory_not_quality_eval",
        "host_machine": platform.machine(),
        "engine_arch": info["Architecture"],
        "engine_cpus": info["NCPU"],
        "engine_memory_bytes": info["MemTotal"],
        "containers": containers,
        "files_sha256": hashes,
        "dataset_manifest_sha256": hashlib.sha256(
            (ROOT / "manifest.json").read_bytes()
        ).hexdigest(),
    }


def main() -> int:
    try:
        result = inventory()
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "details": (
                        "Docker access or required service unavailable; "
                        "no writes attempted"
                    ),
                }
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

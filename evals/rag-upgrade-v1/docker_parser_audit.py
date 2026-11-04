"""用 running Ingestion Worker 的 image/cache 啟動無網路的 U0 暫時容器。

只掛載自製 eval 與唯讀 tokenizer cache；不掛公司資料、.env 或 DB volume。
不 rebuild/pull，不啟動 Worker/CLI/migration，不繼承正式容器的 env/secrets。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from audit_parsers import MODEL_ID, MODEL_REVISION, failures

ROOT = Path(__file__).resolve().parent
CONTAINER_ROOT = "/u0/workspace/evals/rag-upgrade-v1"
CACHE_PATH = "/var/cache/huggingface"


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode or len(result.stdout) > 65_536:
        raise RuntimeError("Docker metadata unavailable")
    return result.stdout


def cache_volume(metadata: dict) -> str:
    matches = [
        m
        for m in metadata["mounts"]
        if m.get("Destination") == CACHE_PATH and m.get("Type") == "volume"
    ]
    if len(matches) != 1:
        raise ValueError("Expected one tokenizer volume")
    name = matches[0]["Name"]
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]+", name
    ):
        raise ValueError("Invalid volume name")
    return name


def command(metadata: dict) -> list[str]:
    image_id = metadata["image_id"]
    user = metadata["user"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Expected immutable image ID")
    if (
        not isinstance(user, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?", user)
        or user.split(":")[0] in ("0", "root")
    ):
        raise ValueError("Expected configured non-root image user")
    if "," in str(ROOT):
        raise ValueError("Unsupported mount path")
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--no-healthcheck",
        "--memory=512m",
        "--cpus=1",
        "--pids-limit=128",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        "--env",
        "HF_HUB_OFFLINE=1",
        "--env",
        "TRANSFORMERS_OFFLINE=1",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        f"HF_HOME={CACHE_PATH}",
        "--mount",
        f"type=bind,source={ROOT},target={CONTAINER_ROOT},readonly",
        "--mount",
        f"type=volume,source={cache_volume(metadata)},target={CACHE_PATH},readonly",
        "--entrypoint",
        "python",
        image_id,
        f"{CONTAINER_ROOT}/audit_parsers.py",
        "--chunks",
        "--no-payloads",
        "--installed-parser-only",
    ]


def main() -> int:
    try:
        ids = docker(
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.project=ingestion-service",
            "--filter",
            "label=com.docker.compose.service=ingestion-worker",
        ).split()
        if len(ids) != 1:
            raise RuntimeError("Expected one running worker")
        # 只讀這三個欄位；不讀取或輸出 Config.Env。
        metadata = json.loads(
            docker(
                "inspect",
                "--format",
                '{"image_id":{{json .Image}},"user":{{json .Config.User}},'
                '"mounts":{{json .Mounts}}}',
                ids[0],
            )
        )
        invocation = command(metadata)
        result = subprocess.run(
            invocation, capture_output=True, text=True, timeout=180, check=False
        )
        if result.returncode not in (0, 1) or len(result.stdout) > 131_072:
            raise RuntimeError("Container audit could not complete")
        report = json.loads(result.stdout)
        if (
            report.get("stage") != "production_parser_chunking_only_not_kis_persistence"
            or report.get("parser_import_mode") != "installed"
            or report.get("payloads_written") is not False
            or report.get("chunking_executed") is not True
            or report.get("model_id") != MODEL_ID
            or report.get("model_revision") != MODEL_REVISION
            or report.get("model_max_length") != 512
            or report.get("status")
            != ("PASSED" if result.returncode == 0 else "FAILED")
            or report.get("failures") != failures(report)
        ):
            raise ValueError("Unexpected audit contract")
        expected_ids = {
            doc["id"] for doc in json.loads((ROOT / "documents.json").read_text())
        }
        received_ids = [item["fixture"] for item in report["results"]]
        if (
            len(received_ids) != len(set(received_ids))
            or set(received_ids) != expected_ids
        ):
            raise ValueError("Unexpected fixture collection")
        report["container_audit"] = {
            "captured_at": datetime.now(UTC).isoformat(),
            "image_id": metadata["image_id"],
            "image_user": metadata["user"],
            "network": "none",
            "container_memory_limit_bytes": 512 * 1024**2,
            "container_cpu_limit": 1,
            "dataset_manifest_sha256": hashlib.sha256(
                (ROOT / "manifest.json").read_bytes()
            ).hexdigest(),
            "audit_script_sha256": hashlib.sha256(
                (ROOT / "audit_parsers.py").read_bytes()
            ).hexdigest(),
            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        # 完整計數報告留在評估 artifact，不讓 Terminal 輸出數百行。
        # 其中只有自製 fixture ID、頁碼、計數和 fingerprint，沒有 chunk 全文。
        destination = ROOT / "runtime" / "u0-parser-chunk-docker.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "details": (
                        "Docker/cache/container unavailable; "
                        "no DB or LLM calls attempted"
                    ),
                }
            )
        )
        return 2
    summary = {key: value for key, value in report.items() if key != "results"}
    all_chunks = [item["chunks"] for item in report["results"] if item["chunks"]]
    summary["chunk_summary"] = {
        "documents_with_chunks": len(all_chunks),
        "total_chunks": sum(c["count"] for c in all_chunks),
        "max_tokens": max((c["max_tokens"] for c in all_chunks), default=0),
        "missing_anchor_documents": sum(
            bool(c["missing_gold_anchors"]) for c in all_chunks
        ),
        "over_limit_count": sum(c["over_limit_count"] for c in all_chunks),
        "recount_mismatch_count": sum(c["recount_mismatch_count"] for c in all_chunks),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

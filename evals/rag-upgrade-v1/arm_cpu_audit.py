"""使用已存在 ARM images／唯讀 cache，驗 installed parser/embedding 的模擬相容性。

不 pull/build，不改 production；兩個 network=none 的暫時容器依序執行。
這不是 Oracle 原生效能、migration/E2E 或新 reranker/parser 候選實跑。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from arm_cpu_probe import MODULES, STABLE_FIELDS, STARTUP_STAGES
from audit_parsers import MODEL_ID, MODEL_REVISION
from docker_parser_audit import CACHE_PATH, CONTAINER_ROOT
from isolated_kis_audit import (
    IMAGE,
    AuditFailure,
    Resources,
    cache_mount,
    command,
    env_flags,
    image_for,
    mounted_tools,
)
from retrieval_metrics_v2 import BASELINE
from validate_revision_v2 import REVISION, validate_at

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[1]
BASELINE_SHA = "e09a15cc10997150bb1fa8f13205e8bc178df3282c95e6ee588659fcfd9724ec"
TAGS = {
    "ingestion": "helpdesk/ingestion-service:arm64",
    "embedding": "helpdesk/knowledge-index-service:arm64",
}
NUMERIC_THREADS = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AuditFailure(code)


def validate_image(metadata: dict) -> None:
    require(
        IMAGE.fullmatch(metadata["image_id"]) is not None
        and metadata["os"] == "linux"
        and metadata["architecture"] == "arm64"
        and isinstance(metadata["user"], str)
        and metadata["user"].split(":")[0] not in {"", "0", "root"},
        "local_non_root_arm_image_required",
    )


def verify_module_hashes(mode: str, hashes: dict) -> None:
    require(set(hashes) == set(MODULES[mode]), "arm_module_set_changed")
    for name, actual in hashes.items():
        package = name.split(".")[0]
        service = package.replace("_", "-")
        path = WORKSPACE / service / "src" / (name.replace(".", "/") + ".py")
        require(
            actual == hashlib.sha256(path.read_bytes()).hexdigest(),
            "arm_image_source_drift",
        )


def verify_snapshot(mode: str, snapshot: dict, baseline: dict) -> None:
    require(
        snapshot["status"] == "PASSED"
        and snapshot["mode"] == mode
        and snapshot["machine"] == "aarch64"
        and snapshot["python"].startswith("3.11."),
        "arm_snapshot_identity",
    )
    verify_module_hashes(mode, snapshot["installed_modules_sha256"])
    require(
        type(snapshot["process_peak_rss_bytes"]) is int
        and snapshot["process_peak_rss_bytes"] > 0,
        "arm_rss_invalid",
    )
    if mode == "ingestion":
        require(
            snapshot["documents"] == 40
            and snapshot["chunks"] == 91
            and snapshot["missing_anchor_count"] == 0
            and snapshot["dataset_manifest_sha256"]
            == baseline["dataset_manifest_sha256"],
            "arm_parser_scope_or_anchor_changed",
        )
        expected = [
            {key: row[key] for key in STABLE_FIELDS} for row in baseline["mapping"]
        ]

        def by_key(row):
            return row["fixture"], row["chunk_index"]

        require(
            sorted(snapshot["records"], key=by_key) == sorted(expected, key=by_key),
            "arm_chunk_content_page_token_parity_failed",
        )
    else:
        require(
            snapshot["dimension"] == 384
            and snapshot["model_id"] == MODEL_ID
            and snapshot["model_revision"] == MODEL_REVISION
            and snapshot["device"] == "cpu"
            and snapshot["cuda_version"] is None
            and snapshot["vector_count"] == 2
            and snapshot["vectors_finite"] is True
            and snapshot["token_over_limit_rejected"] is True,
            "arm_embedding_contract",
        )
        norms = snapshot["vector_norms"]
        require(
            isinstance(norms, list)
            and len(norms) == 2
            and all(
                type(norm) in (int, float)
                and math.isfinite(norm)
                and abs(norm - 1) <= 0.0001
                for norm in norms
            ),
            "arm_vector_norm_invalid",
        )


def decode_startup_events(raw: str | bytes | None) -> list[dict]:
    """拒絕非 allowlist 的 attach 輸出；不轉抄 stderr 或 traceback。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    require(
        isinstance(raw, str) and len(raw.encode()) <= 65_536, "diagnostic_body_bound"
    )
    lines = raw.splitlines()
    require(0 < len(lines) <= len(STARTUP_STAGES), "diagnostic_event_count")
    events = []
    previous = -1
    for index, line in enumerate(lines):
        row = json.loads(line)
        require(isinstance(row, dict), "diagnostic_event_object")
        stage = row.get("diagnostic_stage")
        require(stage == STARTUP_STAGES[index], "diagnostic_stage_order")
        fields = {"diagnostic_stage", "elapsed_seconds"}
        if stage == "source_hashes_ready":
            fields |= {"installed_modules_sha256", "machine", "python"}
            require(
                row["machine"] == "aarch64" and row["python"].startswith("3.11."),
                "diagnostic_arm_identity",
            )
            verify_module_hashes("ingestion", row["installed_modules_sha256"])
        elif stage == "token_count_ready":
            fields |= {"model_max_length", "synthetic_tokens"}
            require(
                type(row["model_max_length"]) is int
                and row["model_max_length"] == 512
                and type(row["synthetic_tokens"]) is int
                and row["synthetic_tokens"] > 0,
                "diagnostic_token_count",
            )
        require(set(row) == fields, "diagnostic_unknown_fields")
        elapsed = row["elapsed_seconds"]
        require(
            type(elapsed) in (int, float)
            and math.isfinite(elapsed)
            and elapsed >= 0
            and elapsed >= previous,
            "diagnostic_elapsed_invalid",
        )
        previous = elapsed
        events.append(row)
    return events


def diagnose_startup(resources: Resources, container: str, report: dict) -> None:
    resources.inspect(container)
    try:
        outgoing = command("start", "--attach", container, timeout=90, limit=65_536)
    except subprocess.TimeoutExpired as exc:
        report["startup_events"] = decode_startup_events(exc.stdout)
        report["last_startup_stage"] = report["startup_events"][-1]["diagnostic_stage"]
        raise AuditFailure("arm_startup_diagnostic_timeout") from None
    events = decode_startup_events(outgoing)
    report["startup_events"] = events
    report["last_startup_stage"] = events[-1]["diagnostic_stage"]
    state = resources.inspect(container)
    require(
        not state["running"]
        and not state["oom"]
        and state["exit"] == 0
        and len(events) == len(STARTUP_STAGES),
        "arm_startup_diagnostic_incomplete",
    )
    report["status"] = "DIAGNOSTIC_ONLY"


def execute(
    resources: Resources,
    report: dict,
    *,
    diagnostic: bool = False,
    single_thread: bool = False,
) -> None:
    validate_at(REVISION)
    endpoint = os.environ.get("DOCKER_HOST") or command(
        "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"
    )
    require(endpoint.startswith("unix:///"), "local_docker_required")
    require(
        hashlib.sha256(BASELINE.read_bytes()).hexdigest() == BASELINE_SHA,
        "arm_baseline_provenance_changed",
    )
    baseline = json.loads(BASELINE.read_text())
    report["docker_host_architecture"] = command(
        "info", "--format", "{{.Architecture}}"
    )
    report["runtime_scope"] = "arm_emulation_on_intel_not_oracle_native_performance"
    # 只讀原 worker 的 cache mount；不繼承 Env、service key、DB 或文件 volume。
    cache_sources = {
        "ingestion": image_for("ingestion-service", "ingestion-worker"),
        "embedding": image_for("knowledge-index-service", "worker"),
    }
    runtime_env = {
        "HF_HOME": CACHE_PATH,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    # 明確的資源診斷實驗，不改正式服務或 ranking/chunking 設定。
    thread_limits = dict.fromkeys(NUMERIC_THREADS, "1") if single_thread else {}
    runtime_env.update(thread_limits)
    report["numeric_thread_environment"] = thread_limits
    offline = env_flags(runtime_env)
    report["snapshots"], report["images"] = {}, {}
    for mode, tag in TAGS.items():
        metadata = json.loads(
            command(
                "image",
                "inspect",
                "--format",
                '{"image_id":{{json .Id}},"os":{{json .Os}},'
                '"architecture":{{json .Architecture}},"user":{{json .Config.User}}}',
                tag,
            )
        )
        validate_image(metadata)
        report["images"][mode] = metadata
        print(json.dumps({"stage": "arm_no_network_" + mode}), flush=True)
        container = resources.create(
            "arm-" + mode,
            metadata["image_id"],
            768 if mode == "ingestion" else 1536,
            [
                "--platform=linux/arm64",
                *offline,
                *mounted_tools(),
                *cache_mount(cache_sources[mode]),
                "--entrypoint",
                "python",
            ],
            [
                f"{CONTAINER_ROOT}/arm_cpu_probe.py",
                "diagnostic-startup" if diagnostic else mode,
            ],
            network="none",
            user=metadata["user"],
        )
        if diagnostic:
            diagnose_startup(resources, container, report)
            return  # 只測 Ingestion startup，不代替完整 40 文件／embedding gate。
        snapshot = resources.run(container, timeout=360)
        report["snapshots"][mode] = snapshot
        verify_snapshot(mode, snapshot, baseline)
    report["status"] = "PASSED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnose-startup", action="store_true")
    parser.add_argument("--single-thread", action="store_true")
    args = parser.parse_args()
    resources = Resources(uuid4().hex)
    started = time.monotonic()
    report = {
        "run_id": resources.run_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "stage": "installed_arm_cpu_adapter_compatibility_not_native_performance",
        "diagnostic_only": args.diagnose_startup,
        "status": "BLOCKED",
        "quality_gate": "BLOCKED",
        "external_llm_calls": 0,
        "model_downloads": 0,
        "db_calls": 0,
        "company_data_touched": False,
        "production_files_changed": 0,
        "dataset_manifest_sha256": hashlib.sha256(
            (REVISION / "manifest.json").read_bytes()
        ).hexdigest(),
        "baseline_file_sha256": BASELINE_SHA,
        "tool_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "arm_cpu_probe.py",
                "arm_cpu_audit.py",
                "revision_v2_payload.py",
                "isolated_kis_audit.py",
            )
        },
    }
    try:
        execute(
            resources,
            report,
            diagnostic=args.diagnose_startup,
            single_thread=args.single_thread,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AuditFailure,
        subprocess.TimeoutExpired,
    ) as exc:
        report["error_type"] = type(exc).__name__
        report["check"] = (
            exc.code if isinstance(exc, AuditFailure) else "arm_audit_unavailable"
        )
        report["diagnostic_categories"] = resources.failure_diagnostics()
    finally:
        remaining = resources.cleanup()
        report["containers"] = resources.audit
        report["cleanup"] = {
            "status": "PASSED" if not remaining else "FAILED",
            "unremoved_exact_ids": remaining,
            "created_containers": len(resources.containers),
            "network_created": False,
        }
        if remaining:
            report["status"] = "BLOCKED"
            report["check"] = "arm_exact_cleanup_failed"
    exit_code = 0 if report["status"] in {"PASSED", "DIAGNOSTIC_ONLY"} else 2
    report["exit_code"] = exit_code
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    prefix = "u0-arm-startup" if args.diagnose_startup else "u0-arm-cpu"
    destination = ROOT / "runtime" / f"{prefix}-{resources.run_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"snapshots", "containers"}
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

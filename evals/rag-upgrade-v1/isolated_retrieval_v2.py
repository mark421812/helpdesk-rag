"""重新建立自己的 synthetic run，執行 installed Retrieval 的 restricted 分支。

沿用 v2 與舊 exact-ID guards；舊 artifact 不改，不拿已清理 UUID 呼叫。
只讀取 running Retrieval 的白名單 limits，不讀 Env/key，不修改任何正式服務。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

from build_revision_v2 import REVISION, ROOT, VERSION
from cgroup_resources_v2 import snapshot as resource_snapshot
from cgroup_resources_v2 import window as resource_window
from docker_parser_audit import CONTAINER_ROOT
from isolated_kis_audit import (
    AuditFailure,
    command,
    env_flags,
    execute,
    image_for,
    mounted_tools,
    stage,
)
from isolated_revision_v2 import TOOLS, RevisionResources
from retrieval_trace_v2 import MODULES, POLICY_FIELDS
from validate_revision_v2 import validate_at


def snapshot_settings() -> dict:
    ids = command(
        "ps",
        "-q",
        "--filter",
        "label=com.docker.compose.project=retrieval-service",
        "--filter",
        "label=com.docker.compose.service=retrieval-api",
    ).split()
    if len(ids) != 1:
        raise AuditFailure("retrieval_policy_source_unavailable")
    code = (
        "import json; "
        "from retrieval_service.infrastructure.config import get_settings; "
        "s=get_settings(); print(json.dumps({key:getattr(s,key) for key in "
        + repr(POLICY_FIELDS)
        + "},allow_nan=False))"
    )
    result = json.loads(
        command("exec", "-e", "PYTHONDONTWRITEBYTECODE=1", ids[0], "python", "-c", code)
    )
    if set(result) != set(POLICY_FIELDS):
        raise AuditFailure("retrieval_policy_snapshot_allowlist")
    return result


class CapturedResources(RevisionResources):
    """純觀察包裝保留父類別驗證，bundle 只存在記憶體。"""

    def __init__(self, run_id):
        super().__init__(run_id)
        self.roles = {}
        self.bundle = None
        self.verified = None
        self.test_env = {}

    def create(self, role, image, memory, args, invocation, **kwargs):
        container_id = super().create(role, image, memory, args, invocation, **kwargs)
        self.roles[role] = container_id
        if role == "verify":
            # 只從本程式剛生成的 flags 取測試 key，不讀正式 Config.Env。
            for index, arg in enumerate(args[:-1]):
                if arg == "--env":
                    key, value = args[index + 1].split("=", 1)
                    if key in {
                        "U0_RUN_ID",
                        "API_INTERNAL_KEY",
                        "PYTHONDONTWRITEBYTECODE",
                    }:
                        self.test_env[key] = value
        return container_id

    def run(self, container_id, *args, **kwargs):
        value = super().run(container_id, *args, **kwargs)
        if container_id == self.roles.get("parser"):
            self.bundle = value
        elif container_id == self.roles.get("verify"):
            self.verified = value
        return value


def main() -> int:
    if os.environ.get("U0_ALLOW_ISOLATED_WRITES") != "1":
        print(
            json.dumps({"status": "BLOCKED", "check": "isolated_write_opt_in_required"})
        )
        return 2
    load_flag = os.environ.get("U0_RETRIEVAL_LOAD")
    if load_flag not in (None, "1"):
        print(json.dumps({"status": "BLOCKED", "check": "load_flag_invalid"}))
        return 2
    load_enabled = load_flag == "1"
    started = time.monotonic()
    resources = CapturedResources(uuid4().hex)
    report = {
        "run_id": resources.run_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "stage": (
            "v2_isolated_retrieval_restricted_load_not_answer_quality"
            if load_enabled
            else "v2_isolated_retrieval_restricted_not_answer_quality"
        ),
        "revision": VERSION,
        "quality_gate": "BLOCKED",
        "company_data_touched": False,
        "external_llm_calls": 0,
        "model_downloads": 0,
        "production_files_changed": 0,
        "dataset_manifest_sha256": hashlib.sha256(
            (REVISION / "manifest.json").read_bytes()
        ).hexdigest(),
        "tools_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                *TOOLS,
                "isolated_retrieval_v2.py",
                "retrieval_trace_v2.py",
                "retrieval_load_v2.py",
                "cgroup_resources_v2.py",
            )
        },
    }
    exit_code = 2
    try:
        endpoint = os.environ.get("DOCKER_HOST") or command(
            "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"
        )
        if not endpoint.startswith("unix:///"):
            raise AuditFailure("local_docker_required")
        validate_at(REVISION)
        snapshot = snapshot_settings()
        retrieval_image = image_for("retrieval-service", "retrieval-api")
        execute(resources, report)
        report["images"]["retrieval"] = retrieval_image["image_id"]
        stage("installed_retrieval_restricted_trace_real_kis_http")
        resources.start(resources.roles["api"])
        roles = ("api", "postgres", "qdrant")
        before = resource_snapshot(resources, roles) if load_enabled else None
        window_start = time.monotonic()
        args = (
            env_flags(
                {
                    **resources.test_env,
                    "HTTP_PROXY": "",
                    "HTTPS_PROXY": "",
                    "ALL_PROXY": "",
                    "NO_PROXY": "kis-api",
                }
            )
            + mounted_tools()
            + ["--entrypoint", "python"]
        )
        trace = resources.create(
            "retrieval-trace",
            retrieval_image["image_id"],
            256,
            args,
            [
                f"{CONTAINER_ROOT}/"
                + ("retrieval_load_v2.py" if load_enabled else "retrieval_trace_v2.py")
            ],
            stdin=True,
        )
        result = resources.run(
            trace,
            {
                "run_id": resources.run_id,
                "bundle": resources.bundle,
                "verified": resources.verified,
                "settings": snapshot,
            },
            timeout=360,
        )
        if load_enabled:
            after = resource_snapshot(resources, roles)
            report["resource_window"] = resource_window(
                before, after, time.monotonic() - window_start
            )
        resources.stop(resources.roles["api"])
        # 只比對被執行的 installed 模組，不宣稱整個 image 等於 host source。
        expected = {
            name: hashlib.sha256(
                (
                    ROOT.parents[1]
                    / "retrieval-service/src/retrieval_service"
                    / (name.replace(".", "/") + ".py")
                ).read_bytes()
            ).hexdigest()
            for name in MODULES
        }
        if (
            result["installed_modules_sha256"] != expected
            or result["settings"] != snapshot
        ):
            raise AuditFailure("retrieval_installed_source_or_policy_changed")
        report["retrieval_trace"] = result
        exit_code = 0
    except (
        OSError,
        ValueError,
        KeyError,
        AuditFailure,
        subprocess.TimeoutExpired,
    ) as exc:
        report.update(
            {
                "status": "BLOCKED",
                "error_type": type(exc).__name__,
                "check": exc.code
                if isinstance(exc, AuditFailure)
                else "retrieval_audit_contract",
                "diagnostic_categories": resources.failure_diagnostics(),
            }
        )
    finally:
        stage("exact_id_cleanup")
        failures = resources.cleanup()
        report["cleanup"] = {
            "status": "FAILED" if failures else "PASSED",
            "unremoved_exact_ids": failures,
            "created_containers": len(resources.containers),
            "network_created": resources.network is not None,
        }
        report["containers"] = resources.audit
    if failures:
        report["status"] = "BLOCKED"
        exit_code = 2
    report["exit_code"] = exit_code
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    destination = (
        ROOT
        / "runtime"
        / (
            f"u0-v2-retrieval-restricted-{'load-' if load_enabled else ''}"
            f"{resources.run_id}.json"
        )
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        key: value
        for key, value in report.items()
        if key
        not in {
            "mapping",
            "containers",
            "anchor_mapping",
            "chunk_observations",
            "tuning_candidates",
            "retrieval_trace",
        }
    }
    for field in ("tuning_candidates", "retrieval_trace"):
        if field in report:
            summary[field] = {
                key: value for key, value in report[field].items() if key != "records"
            }
    summary["report_path"] = str(destination)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

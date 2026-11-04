"""沿用已驗證隔離 lifecycle，只替換 v2 producer/probe；不覆寫 v1 工具。

9 個本次資源以 exact ID/run label 管理；無 host port、internal network、
tmpfs DB、唯讀 cache、log-driver=none。暫不使用 Retrieval/Answer/Groq。
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
from docker_parser_audit import CONTAINER_ROOT
from isolated_kis_audit import AuditFailure, Resources, execute, stage
from validate_revision_v2 import validate_at

TOOLS = (
    "isolated_revision_v2.py",
    "revision_v2_payload.py",
    "revision_v2_probe.py",
    "isolated_kis_audit.py",
    "index_fixtures.py",
    "kis_persistence_probe.py",
    "audit_parsers.py",
    "audit_revision_v2.py",
    "docker_parser_audit.py",
    "validate_revision_v2.py",
    "build_revision_v2.py",
    "build_dataset.py",
    "runner.py",
    "tuning_candidates_v2.py",
)


def invocation_for(role: str, invocation: list[str]) -> list[str]:
    if role == "parser":
        if invocation != [f"{CONTAINER_ROOT}/index_fixtures.py"]:
            raise AuditFailure("unexpected_revision_producer")
        return [f"{CONTAINER_ROOT}/revision_v2_payload.py"]
    if role in {"submit", "wait", "verify"}:
        if invocation != [f"{CONTAINER_ROOT}/kis_persistence_probe.py", role]:
            raise AuditFailure("unexpected_revision_probe")
        return [f"{CONTAINER_ROOT}/revision_v2_probe.py", role]
    return invocation


class RevisionResources(Resources):
    """有限的 invocation 投影，不繞過父類別的 mount/network/ownership 驗證。"""

    def create(self, role, image, memory, args, invocation, **kwargs):
        return super().create(
            role, image, memory, args, invocation_for(role, invocation), **kwargs
        )


def main() -> int:
    if os.environ.get("U0_ALLOW_ISOLATED_WRITES") != "1":
        print(
            json.dumps({"status": "BLOCKED", "check": "isolated_write_opt_in_required"})
        )
        return 2
    run_id = uuid4().hex
    resources = RevisionResources(run_id)
    started = time.monotonic()
    report = {
        "run_id": run_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "stage": "v2_isolated_kis_persistence_not_answer_quality",
        "revision": VERSION,
        "quality_gate": "BLOCKED",
        "company_data_touched": False,
        "external_llm_calls": 0,
        "model_downloads": 0,
        "production_files_changed": 0,
        "dataset_manifest_sha256": hashlib.sha256(
            (REVISION / "manifest.json").read_bytes()
        ).hexdigest(),
        "parent_manifest_sha256": hashlib.sha256(
            (ROOT / "manifest.json").read_bytes()
        ).hexdigest(),
        "tools_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in TOOLS
        },
    }
    exit_code = 2
    try:
        validate_at(REVISION)
        execute(resources, report)
        if report.get("revision") != VERSION or report.get("status") != "PASSED":
            raise AuditFailure("v2_report_binding_failed")
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
                else "revision_audit_contract",
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
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["exit_code"] = exit_code
    destination = ROOT / "runtime" / f"u0-v2-kis-persistence-{run_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        k: v
        for k, v in report.items()
        if k
        not in {
            "mapping",
            "containers",
            "anchor_mapping",
            "chunk_observations",
            "tuning_candidates",
        }
    }
    if "tuning_candidates" in report:
        summary["tuning_candidates"] = {
            key: value
            for key, value in report["tuning_candidates"].items()
            if key != "records"
        }
    summary["report_path"] = str(destination)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

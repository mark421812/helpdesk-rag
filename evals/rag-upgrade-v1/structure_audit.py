"""將無網路結構 probe 重新產生的 hash 綁到已完成的隔離 KIS 入庫證據。

不重建知識庫、不連 DB、不呼叫 LLM；歷史證據不是現在仍可查詢的資料。
只有相同 corpus／tools／immutable images／全部 canonical hashes 才接受關聯。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from docker_parser_audit import CACHE_PATH, CONTAINER_ROOT
from isolated_kis_audit import (
    AuditFailure,
    Resources,
    cache_mount,
    command,
    env_flags,
    image_for,
    mounted_tools,
)
from runner import validate

ROOT = Path(__file__).resolve().parent
EVIDENCE_NAME = "u0-kis-persistence-3a26cddc942b47b6bc4debaa4d84e833.json"
EVIDENCE_SHA256 = "618d5862a6c2cad36deb2e3783917b9714ec19d4ccc2afc2671b7c33f028d518"
STRUCTURAL_FIELDS = {
    "block_type",
    "block_id",
    "table_id",
    "row_id",
    "rows",
    "cells",
    "column_names",
    "source_order",
    "continuation_id",
    "row_span",
    "col_span",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AuditFailure(code)


def validate_evidence(evidence: dict, manifest_sha: str, tools: dict) -> None:
    require(
        evidence.get("status") == "PASSED"
        and evidence.get("exit_code") == 0
        and evidence.get("stage") == "isolated_kis_persistence_not_answer_quality"
        and evidence.get("dataset_manifest_sha256") == manifest_sha
        and evidence.get("tools_sha256") == tools
        and evidence.get("company_data_touched") is False
        and evidence.get("external_llm_calls") == 0
        and evidence.get("model_downloads") == 0
        and evidence.get("postgres_major") == 16
        and evidence.get("migration", {}).get("exit_code") == 0
        and evidence.get("canonical_mismatches") == 0
        and evidence.get("jobs_completed") == 40
        and evidence.get("jobs_failed") == 0
        and evidence.get("documents") == 40
        and evidence.get("versions") == 40
        and evidence.get("chunks") == evidence.get("vector_points") == 80
        and evidence.get("vector_dimension") == 384,
        "persistence_evidence_not_valid",
    )
    cleanup = evidence.get("cleanup", {})
    containers = evidence.get("containers", [])
    require(
        cleanup.get("status") == "PASSED"
        and cleanup.get("unremoved_exact_ids") == []
        and cleanup.get("created_containers") == len(containers) == 9
        and all(c.get("log_driver") == "none" for c in containers),
        "persistence_isolation_evidence_not_valid",
    )


def bind_chunks(
    documents: list[dict], evidence: dict, expected_ids: set[str]
) -> list[dict]:
    identifiers = [d["fixture"] for d in documents]
    require(
        len(identifiers) == len(set(identifiers)) == len(expected_ids)
        and set(identifiers) == expected_ids,
        "structure_fixture_collection_incomplete",
    )
    entries = evidence["mapping"]
    require(len(entries) == evidence["chunks"], "mapping_count_mismatch")
    mapped = {}
    chunk_ids = set()
    fixture_bindings = {}
    for entry in entries:
        key = entry["fixture"], entry["chunk_index"]
        require(
            key not in mapped and key[0] in expected_ids, "mapping_duplicate_or_foreign"
        )
        require(type(key[1]) is int and key[1] >= 0, "mapping_index_invalid")
        for field in ("chunk_id", "document_id", "version_id"):
            require(str(UUID(entry[field])) == entry[field], "mapping_uuid_invalid")
        require(entry["chunk_id"] not in chunk_ids, "mapping_chunk_uuid_duplicate")
        chunk_ids.add(entry["chunk_id"])
        binding = entry["document_id"], entry["version_id"]
        require(
            fixture_bindings.get(key[0], binding) == binding,
            "mapping_version_binding_mismatch",
        )
        fixture_bindings[key[0]] = binding
        mapped[key] = entry
    actual = set()
    for document in documents:
        for index, chunk in enumerate(document["chunks"]):
            key = document["fixture"], chunk["index"]
            require(
                chunk["index"] == index and key not in actual, "chunk_order_invalid"
            )
            actual.add(key)
            require(key in mapped, "canonical_mapping_missing")
            entry = mapped[key]
            for source, target in (
                ("content_sha256", "content_sha256"),
                ("section_sha256", "section_sha256"),
                ("page", "page_number"),
                ("tokens", "token_count"),
            ):
                require(chunk[source] == entry[target], "canonical_mapping_changed")
            # 歷史 DB 已清理；這個 ID 只供 evidence provenance，不是可用 endpoint。
            chunk["historical_chunk_id"] = entry["chunk_id"]
        require(bool(document["chunks"]), "empty_structure_chunks")
    require(actual == set(mapped), "canonical_mapping_collection_incomplete")
    return documents


def summarize(documents: list[dict], kis: dict, ingestion: dict) -> dict:
    formats = {}
    for extension in (".pdf", ".docx", ".md", ".txt"):
        own = [d for d in documents if d["format"] == extension]
        chunks = [c for d in own for c in d["chunks"]]
        parser_rows = [r for d in own for b in d["blocks"] for r in b["rows"]]
        chunk_rows = [r for c in chunks for r in c["rows"]]
        formats[extension] = {
            "documents": len(own),
            "chunks": len(chunks),
            "expected_row_occurrences": sum(len(d["expected_rows"]) for d in own),
            "parser_row_occurrences": len(parser_rows),
            "chunk_row_occurrences": len(chunk_rows),
            "parser_linear_cell_order_verified": sum(
                r["linear_text_cell_order"] for r in parser_rows
            ),
            "chunk_linear_cell_order_verified": sum(
                r["linear_text_cell_order"] for r in chunk_rows
            ),
            "parser_table_blocks": sum(d["block_types"].get("table", 0) for d in own),
            "chunks_with_sections": sum(c["has_section"] for c in chunks),
            "chunks_with_pages": sum(c["page"] is not None for c in chunks),
            "header_occurrences_in_chunks": sum(
                c["header_occurrences"] for c in chunks
            ),
            "header_cell_order_chunks": sum(c["header_cell_order"] for c in chunks),
        }
    gap_counts = Counter()
    steps_verified = 0
    for doc in documents:
        for stage in ("blocks", "chunks"):
            anchors = [r["anchor"] for c in doc[stage] for r in c["rows"]]
            if Counter(anchors) != Counter(doc["expected_rows"]):
                gap_counts[f"{stage}_row_occurrences_not_exact"] += 1
        steps = [s["anchor"] for c in doc["chunks"] for s in c["steps"]]
        if steps != doc["expected_steps"]:
            gap_counts["step_order_not_exact"] += 1
        else:
            steps_verified += len(steps)
    return {
        "formats": formats,
        "text_observation_gaps": dict(sorted(gap_counts.items())),
        "ordered_step_anchors_verified": steps_verified,
        "structural_names_at_chunk_boundary": {
            "ingestion_chunk": sorted(
                STRUCTURAL_FIELDS & set(ingestion["chunk_fields"])
            ),
            "kis_request": sorted(STRUCTURAL_FIELDS & set(kis["chunk_request_fields"])),
            "kis_chunk_columns": sorted(STRUCTURAL_FIELDS & set(kis["chunk_columns"])),
            "kis_search_response": sorted(
                STRUCTURAL_FIELDS & set(kis["search_result_fields"])
            ),
        },
        "parser_fields_not_in_chunk": sorted(
            set(ingestion["parsed_block_fields"]) - set(ingestion["chunk_fields"])
        ),
        "limits": [
            "linear_text_not_typed_rows_or_cells",
            "no_proof_of_generic_json_metadata_structure_contract",
            "no_cross_page_table_identity_or_continuation_validation",
            "no_human_gold_or_visual_qa_approval",
            "historical_persistence_not_live_database_recheck",
            "small_three_row_fixture_not_large_or_incomplete_list_coverage",
        ],
    }


def execute(resources: Resources, report: dict) -> None:
    validate()
    path = ROOT / "runtime" / EVIDENCE_NAME
    require(sha(path) == EVIDENCE_SHA256, "persistence_artifact_hash_changed")
    evidence = json.loads(path.read_text())
    tools = {name: sha(ROOT / name) for name in evidence["tools_sha256"]}
    validate_evidence(evidence, sha(ROOT / "manifest.json"), tools)
    endpoint = os.environ.get("DOCKER_HOST") or command(
        "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"
    )
    require(endpoint.startswith("unix:///"), "local_docker_required")
    images = {
        "ingestion": image_for("ingestion-service", "ingestion-worker"),
        "kis": image_for("knowledge-index-service", "api"),
    }
    for name, metadata in images.items():
        require(
            metadata["image_id"] == evidence["images"][name], "evidence_image_changed"
        )
    report["images"] = {name: value["image_id"] for name, value in images.items()}
    report["persistence_provenance"] = {
        "artifact": EVIDENCE_NAME,
        "sha256": EVIDENCE_SHA256,
        "captured_at": evidence["captured_at"],
        "run_id": evidence["run_id"],
        "historical_only": True,
    }
    offline = env_flags(
        {
            "HF_HOME": CACHE_PATH,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    snapshots = {}
    for mode in ("ingestion", "kis"):
        args = offline + mounted_tools() + ["--entrypoint", "python"]
        if mode == "ingestion":
            args += cache_mount(images[mode])
        container = resources.create(
            f"structure-{mode}",
            images[mode]["image_id"],
            512 if mode == "ingestion" else 256,
            args,
            [f"{CONTAINER_ROOT}/structure_probe.py", mode],
            network="none",
            user=images[mode]["user"],
        )
        snapshots[mode] = resources.run(container, timeout=240)
        require(snapshots[mode]["mode"] == mode, "structure_snapshot_mode_invalid")
    ing, kis = snapshots["ingestion"], snapshots["kis"]
    expected = {d["id"] for d in json.loads((ROOT / "documents.json").read_text())}
    documents = bind_chunks(ing.pop("documents"), evidence, expected)
    report["schema_snapshots"] = snapshots
    report["documents"] = documents
    report["summary"] = summarize(documents, kis, ing)
    report["inventory_status"] = "PASSED"


def main() -> int:
    run_id = uuid4().hex
    resources = Resources(run_id)
    started = time.monotonic()
    report = {
        "run_id": run_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "stage": "structure_inventory_linked_to_historical_kis_persistence",
        "quality_gate": "BLOCKED",
        "dataset_manifest_sha256": sha(ROOT / "manifest.json"),
        "tool_sha256": {
            name: sha(ROOT / name)
            for name in (
                "structure_audit.py",
                "structure_probe.py",
                "isolated_kis_audit.py",
                "index_fixtures.py",
                "docker_parser_audit.py",
                "runner.py",
            )
        },
        "company_data_touched": False,
        "external_llm_calls": 0,
        "database_connections": 0,
        "model_downloads": 0,
    }
    try:
        execute(resources, report)
        exit_code = 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AuditFailure,
        subprocess.TimeoutExpired,
    ) as exc:
        report["inventory_status"] = "BLOCKED"
        report["error_type"] = type(exc).__name__
        report["check"] = (
            exc.code
            if isinstance(exc, AuditFailure)
            else "structure_environment_or_contract"
        )
        exit_code = 2
    finally:
        remaining = resources.cleanup()
        report["containers"] = resources.audit
        report["cleanup"] = {
            "status": "FAILED" if remaining else "PASSED",
            "unremoved_exact_ids": remaining,
            "created_containers": len(resources.containers),
            "network_created": resources.network is not None,
        }
    if remaining:
        report["inventory_status"] = "BLOCKED"
        exit_code = 2
    report["exit_code"] = exit_code
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    destination = ROOT / "runtime" / f"u0-structure-audit-{run_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    output = {
        k: v
        for k, v in report.items()
        if k not in ("documents", "containers", "schema_snapshots")
    }
    output["report_path"] = str(destination)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

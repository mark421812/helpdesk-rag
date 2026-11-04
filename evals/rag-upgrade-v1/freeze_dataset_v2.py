"""凍結已由真人核准的 v2 資料／Gold／tuning relevance，不代簽或評品質。

快照只保存 hash、case ID 與 counts，不轉抄真人、notes 或文件原文。
這不是 runtime/model/prompt/policy freeze；後續驗收仍須逐次 verify，不能
把一份舊快照當作已變動資料的核准。預設 pending template 必須拒絕寫入。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from gold_review_v2 import expected_rows
from gold_review_v2 import validate as validate_gold
from retrieval_metrics_v2 import (
    BASELINE,
    LABELS,
    PROTOCOL,
    ROOT,
    digest,
    rankings,
    read_record,
    require,
    validate_labels,
)
from validate_revision_v2 import REVISION, validate_at

SOURCE_CANONICAL_SHA256 = (
    "556b169f7c3bde6a2d002ff6eed00e3feaf1f3ecfd25447a3fccd527e9879234"
)
SCHEMA = "u0-dataset-human-judgments-freeze-v1"
GOLD = ROOT / "gold-review-v2.template.json"


def support_fingerprints() -> dict[str, str]:
    """標註規則或驗證程式變更也須重新資格確認，不只比較 protocol 名字。"""
    names = (
        "relevance-protocol-v1.md",
        "gold_review_v2.py",
        "retrieval_metrics_v2.py",
        "validate_revision_v2.py",
        "freeze_dataset_v2.py",
    )
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names
    }


def readiness(gold: dict, labels: dict, report: dict) -> dict:
    """沿用既有完整 validator，不以 status 字串或檔案存在取代核對。"""
    validate_at(REVISION)
    require(digest(report) == SOURCE_CANONICAL_SHA256, "source_report_changed")
    stages, depths = rankings(report)
    gold_result = validate_gold(gold)
    label_result, _ = validate_labels(labels, report)
    return {
        "stage": "dataset_freeze_readiness_not_semantic_certification",
        "gold_approved": gold_result["approved"],
        "gold_pending": gold_result["pending"],
        "gold_changes_requested": gold_result["changes_requested"],
        "relevance_approved": label_result["approved"],
        "relevance_pending": label_result["pending"],
        "relevance_changes_requested": label_result["changes_requested"],
        "ready": gold_result["record_complete"] and label_result["complete"],
        "trace_observations": {key: len(rows) for key, rows in stages.items()},
        "trace_candidate_depths": depths,
        "quality_gate": "BLOCKED",
        "semantic_review_performed_by_tool": False,
        "runtime_configuration": "NOT_FROZEN",
        "generation_quality": "NOT_MEASURED",
    }


def snapshot(gold: dict, labels: dict, report: dict, frozen_at: str) -> dict:
    result = readiness(gold, labels, report)
    require(result["ready"] is True, "human_judgments_pending")
    require(isinstance(frozen_at, str), "freeze_time_invalid")
    stamp = datetime.fromisoformat(frozen_at)
    require(
        stamp.tzinfo is not None
        and stamp.utcoffset() == UTC.utcoffset(stamp)
        and stamp.isoformat() == frozen_at,
        "freeze_time_requires_canonical_utc",
    )
    record = {
        "schema_version": SCHEMA,
        "scope": "dataset_gold_and_tuning_relevance_only",
        "frozen_at": frozen_at,
        "dataset_manifest_file_sha256": hashlib.sha256(
            (REVISION / "manifest.json").read_bytes()
        ).hexdigest(),
        "source_report_canonical_sha256": digest(report),
        "gold_review_canonical_sha256": digest(gold),
        "tuning_relevance_canonical_sha256": digest(labels),
        "catalog_canonical_sha256": labels["catalog_sha256"],
        "relevance_protocol": PROTOCOL,
        "support_files_sha256": support_fingerprints(),
        "cases": sorted(
            [
                {key: row[key] for key in ("case_id", "split", "case_sha256")}
                for row in expected_rows()
            ],
            key=lambda row: row["case_id"],
        ),
        "gold_approved": 80,
        "tuning_relevance_approved": 40,
        "source_external_llm_allowed": False,
        "semantic_review_performed_by_tool": False,
        "runtime_configuration": "NOT_FROZEN",
        "generation_quality": "NOT_MEASURED",
        "quality_gate": "BLOCKED",
    }
    return {**record, "snapshot_id": digest(record)}


def verify(record: dict, gold: dict, labels: dict, report: dict) -> None:
    """重建完整預期快照；未知欄位、型別或任何當前輸入變動都拒絕。"""
    expected = snapshot(gold, labels, report, record["frozen_at"])
    # canonical JSON 比較能區分 true/1、false/0，dict == 無法做到。
    require(digest(record) == digest(expected), "snapshot_or_inputs_changed")


def _valid_id(value: str) -> None:
    require(
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value),
        "snapshot_id_invalid",
    )


@contextmanager
def _directory(root: Path, *, create: bool):
    """固定 runtime/frozen；逐層 dir_fd/NOFOLLOW，拒絕 symlink 與越界。"""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ("runtime", "frozen"):
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    require(
                        stat.S_ISDIR(
                            os.stat(
                                name, dir_fd=descriptor, follow_symlinks=False
                            ).st_mode
                        ),
                        "snapshot_directory_not_regular",
                    )
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def freeze(
    gold: dict,
    labels: dict,
    report: dict,
    *,
    root: Path = ROOT,
    frozen_at: str | None = None,
) -> dict:
    """先完整核對才建目錄；原子 no-clobber 發布，只清理自己建立的 temp。"""
    record = snapshot(gold, labels, report, frozen_at or datetime.now(UTC).isoformat())
    _valid_id(record["snapshot_id"])
    payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
    require(len(payload) <= 2_000_000, "snapshot_bound")
    with _directory(root, create=True) as directory:
        name = "." + uuid4().hex + ".tmp"
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode=0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # 寫入途中若來源改變，完整 validator/hash 再查仍須通過。
            verify(record, gold, labels, report)
            os.link(
                name,
                record["snapshot_id"] + ".json",
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            os.fsync(directory)
        finally:
            os.unlink(name, dir_fd=directory)
    return {
        "stage": "dataset_records_frozen_not_u0_pass",
        "snapshot_id": record["snapshot_id"],
        "snapshot_file_sha256": hashlib.sha256(payload).hexdigest(),
        "gold_approved": 80,
        "tuning_relevance_approved": 40,
        "quality_gate": "BLOCKED",
        "runtime_configuration": "NOT_FROZEN",
        "generation_quality": "NOT_MEASURED",
        "semantic_review_performed_by_tool": False,
    }


def read_snapshot(snapshot_id: str, *, root: Path = ROOT) -> dict:
    _valid_id(snapshot_id)
    with _directory(root, create=False) as directory:
        descriptor = os.open(
            snapshot_id + ".json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "snapshot_type")
            raw = stream.read(2_000_001)
    require(len(raw) <= 2_000_000, "snapshot_bound")

    def pairs(items):
        result = dict(items)
        require(len(result) == len(items), "snapshot_duplicate_json_key")
        return result

    def invalid_constant(_value):
        raise ValueError("snapshot_nonfinite_json_number")

    record = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    require(isinstance(record, dict), "snapshot_root")
    require(record["snapshot_id"] == snapshot_id, "snapshot_filename_binding")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "freeze", "verify"))
    parser.add_argument("--gold-review", type=Path, default=GOLD)
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--snapshot-id")
    args = parser.parse_args(argv)
    try:
        report, gold, labels = (
            read_record(BASELINE),
            read_record(args.gold_review),
            read_record(args.labels),
        )
        result = readiness(gold, labels, report)
        if result["ready"] is not True:
            result["status"] = "BLOCKED"
            result["freeze"] = "NOT_PERFORMED"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2
        if args.action == "freeze":
            require(args.snapshot_id is None, "freeze_does_not_accept_existing_id")
            result = freeze(gold, labels, report)
        elif args.action == "verify":
            record = read_snapshot(args.snapshot_id)
            verify(record, gold, labels, report)
            result["snapshot_id"] = args.snapshot_id
            result["freeze"] = "VERIFIED_DATASET_RECORDS_ONLY"
        else:
            require(args.snapshot_id is None, "check_does_not_accept_existing_id")
            result["freeze"] = "NOT_PERFORMED"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        # 不印 exception、reviewer、notes、path 或原始輸入。
        print(json.dumps({"status": "BLOCKED", "check": "invalid_dataset_freeze"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

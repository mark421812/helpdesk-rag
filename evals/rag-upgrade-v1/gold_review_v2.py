"""人工 Gold review 的 hash-bound 表單與唯讀驗證；不核准任何題。

每題 reviewer/checks 由真人核對来源後填写。結構驗證無法證明填表人真的
看過文件，也不冒充獨立 review、品質分數、正式 freeze 或 U0 PASS。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REVISION = ROOT / "revisions/u0-synthetic-v2"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def expected_rows() -> list[dict]:
    rows = []
    for split in ("tuning", "holdout"):
        cases = json.loads((REVISION / "datasets" / f"{split}.json").read_text())
        for case in cases:
            checks = [
                "question_scope",
                "answer_or_refusal",
                "evidence_support",
                "citation_source",
            ]
            if case["gold"]["complete_required"] or not case["gold"]["source_complete"]:
                checks.append("scope_completeness")
            if case["type"] in {"list", "count"}:
                checks.append("unit_and_dedup_key")
            if case["type"] == "followup":
                checks.append("history_reference")
            rows.append(
                {
                    "case_id": case["id"],
                    "split": split,
                    "case_sha256": hashlib.sha256(
                        json.dumps(
                            case,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    "status": "pending",
                    "reviewer": None,
                    "reviewed_at": None,
                    "actor_kind": None,
                    "independent_from_implementation": None,
                    "checks": {check: None for check in checks},
                    "notes": "",
                }
            )
    return rows


def template() -> dict:
    return {
        "schema_version": "u0-human-gold-review-v1",
        "dataset_manifest_sha256": hashlib.sha256(
            (REVISION / "manifest.json").read_bytes()
        ).hexdigest(),
        "cases": expected_rows(),
    }


def validate(record: dict) -> dict:
    expected = template()
    require(set(record) == set(expected), "Review root fields changed")
    require(
        record["schema_version"] == expected["schema_version"], "Unknown review schema"
    )
    require(
        record["dataset_manifest_sha256"] == expected["dataset_manifest_sha256"],
        "Review dataset changed",
    )
    rows = record["cases"]
    require(
        isinstance(rows, list) and len(rows) == 80, "Review must contain all 80 cases"
    )
    by_id = {row["case_id"]: row for row in rows}
    require(
        len(by_id) == 80 and set(by_id) == {r["case_id"] for r in expected["cases"]},
        "Duplicate/missing/foreign review case",
    )
    for base in expected["cases"]:
        row = by_id[base["case_id"]]
        require(set(row) == set(base), "Review case fields changed")
        require(
            row["split"] == base["split"] and row["case_sha256"] == base["case_sha256"],
            "Review case source changed",
        )
        require(
            row["status"] in {"pending", "approved", "changes_requested"},
            "Invalid review status",
        )
        require(
            isinstance(row["notes"], str) and len(row["notes"]) <= 4000,
            "Review notes invalid",
        )
        checks = row["checks"]
        require(
            isinstance(checks, dict) and set(checks) == set(base["checks"]),
            "Required review checks changed",
        )
        require(
            all(v is None or type(v) is bool for v in checks.values()),
            "Review checks must be actual booleans",
        )
        if row["status"] == "pending":
            continue
        require(
            row["actor_kind"] == "human"
            and isinstance(row["reviewer"], str)
            and bool(row["reviewer"].strip()),
            "Human reviewer required",
        )
        require(
            type(row["independent_from_implementation"]) is bool,
            "Review independence must be boolean",
        )
        require(isinstance(row["reviewed_at"], str), "Review time required")
        stamp = datetime.fromisoformat(row["reviewed_at"])
        require(stamp.tzinfo is not None, "Review time must include timezone")
        if row["split"] == "holdout":
            require(
                row["independent_from_implementation"] is True,
                "Holdout requires independent reviewer",
            )
        if row["status"] == "approved":
            require(
                all(value is True for value in checks.values()),
                "Approved case has unchecked requirements",
            )
        else:
            require(
                bool(row["notes"].strip()), "Changes requested must explain finding"
            )
    counts = Counter(row["status"] for row in rows)
    return {
        "stage": "human_review_record_structure_not_semantic_certification",
        "dataset_manifest_sha256": expected["dataset_manifest_sha256"],
        "cases": 80,
        "approved": counts["approved"],
        "pending": counts["pending"],
        "changes_requested": counts["changes_requested"],
        "record_complete": counts["approved"] == 80,
        "semantic_review_performed_by_tool": False,
        "quality_gate": "BLOCKED",
        "freeze": "NOT_PERFORMED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review", type=Path, default=ROOT / "gold-review-v2.template.json"
    )
    args = parser.parse_args()
    try:
        result = validate(json.loads(args.review.read_text()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0  # 只代表結構有效；pending / quality BLOCKED 仍明列。
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "BLOCKED", "check": "invalid_human_review_record"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

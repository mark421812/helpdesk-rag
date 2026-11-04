"""U0 tuning 的 hash-bound 人工相關性表與正式檢索指標；不呼叫服務。

這是離線評估工具，不修改 Retrieval，不把 score/anchor 自動轉成人工等級。
default_grade 必須由真人明確填 0 並核對完整 catalog；未填不是不相關。
入口召回按必要入口群組計算，等價 chunk 只需命中一個；不是完整列充分率。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import UUID

from gold_review_v2 import expected_rows
from gold_review_v2 import validate as validate_gold
from validate_revision_v2 import REVISION, validate_at

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / (
    "runtime/u0-v2-retrieval-restricted-load-a7140183cdf74f999c329ffc8f2f767d.json"
)
LABELS = ROOT / "tuning-relevance-v2.template.json"
SCHEMA = "u0-tuning-chunk-relevance-v1"
PROTOCOL = "human-full-catalog-grades-0-3-entry-equivalence-groups-v1"
CATALOG_FIELDS = (
    "fixture",
    "chunk_index",
    "page_number",
    "token_count",
    "content_sha256",
    "section_sha256",
)
MAPPING_FIELDS = {*CATALOG_FIELDS, "document_id", "version_id", "chunk_id"}


def require(condition: bool, code: str) -> None:
    """錯誤只用固定 code，避免 notes、path 或任意輸入進終端。"""
    if not condition:
        raise ValueError(code)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def read_record(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(2_000_001)
    require(len(raw) <= 2_000_000, "input_bound")

    # 不接受重複欄位／NaN/Infinity；json.loads 預設會悄悄接受它們。
    def pairs(items):
        result = dict(items)
        require(len(result) == len(items), "duplicate_json_key")
        return result

    def invalid_constant(_value):
        raise ValueError("nonfinite_json_number")

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    require(isinstance(result, dict), "record_root_not_object")
    return result


def tuning_cases() -> list[dict]:
    validate_at(REVISION)
    return json.loads((REVISION / "datasets/tuning.json").read_text())


def source_catalog(report: dict) -> tuple[list[dict], dict[str, str]]:
    """穩定 identity 不含已清理的 run UUID，parser/chunk bytes 改變即失效。"""
    require(
        report["status"] == "PASSED"
        and report["exit_code"] == 0
        and report["dataset_manifest_sha256"]
        == hashlib.sha256((REVISION / "manifest.json").read_bytes()).hexdigest()
        and report["company_data_touched"] is False
        and report["external_llm_calls"] == 0
        and report["production_files_changed"] == 0
        and report["canonical_mismatches"] == 0
        and report["jobs_failed"] == 0
        and report["jobs_completed"] == 40
        and report["postgres_major"] == 16
        and report["cleanup"]["status"] == "PASSED",
        "source_report_not_verified_isolated_v2",
    )
    documents = json.loads((REVISION / "documents.json").read_text())
    fixtures = {row["id"] for row in documents}
    mapping = report["mapping"]
    require(isinstance(mapping, list) and len(mapping) == 91, "mapping_count")
    catalog, uuid_to_key = [], {}
    for row in mapping:
        require(isinstance(row, dict) and set(row) == MAPPING_FIELDS, "mapping_fields")
        require(
            row["fixture"] in fixtures
            and type(row["chunk_index"]) is int
            and row["chunk_index"] >= 0
            and type(row["token_count"]) is int
            and 0 < row["token_count"] <= 512
            and (
                row["page_number"] is None
                or type(row["page_number"]) is int
                and row["page_number"] > 0
            ),
            "mapping_source_invalid",
        )
        for field in ("content_sha256", "section_sha256"):
            require(
                isinstance(row[field], str)
                and len(row[field]) == 64
                and all(c in "0123456789abcdef" for c in row[field]),
                "mapping_hash_invalid",
            )
        for field in ("document_id", "version_id", "chunk_id"):
            require(
                isinstance(row[field], str) and str(UUID(row[field])) == row[field],
                "mapping_uuid_invalid",
            )
        stable = {field: row[field] for field in CATALOG_FIELDS}
        key = digest(stable)
        require(
            isinstance(row["chunk_id"], str) and row["chunk_id"] not in uuid_to_key,
            "mapping_duplicate_id",
        )
        uuid_to_key[row["chunk_id"]] = key
        catalog.append({"chunk_key": key, **stable})
    require(len(set(uuid_to_key.values())) == 91, "mapping_duplicate_identity")
    require(
        len({(r["fixture"], r["chunk_index"]) for r in catalog}) == 91,
        "mapping_duplicate_ordinal",
    )
    require({row["fixture"] for row in catalog} == fixtures, "mapping_missing_source")
    return sorted(
        catalog, key=lambda row: (row["fixture"], row["chunk_index"])
    ), uuid_to_key


def template(report: dict) -> dict:
    catalog, _ = source_catalog(report)
    return {
        "schema_version": SCHEMA,
        "protocol": PROTOCOL,
        "dataset_manifest_sha256": report["dataset_manifest_sha256"],
        "catalog_sha256": digest(catalog),
        "catalog": catalog,
        "cases": [
            {
                "case_id": row["case_id"],
                "split": "tuning",
                "case_sha256": row["case_sha256"],
                "status": "pending",
                "actor_kind": None,
                "reviewer": None,
                "reviewed_at": None,
                "full_catalog_reviewed": None,
                "protocol_accepted": None,
                "default_grade": None,
                "grade_overrides": [],
                "entry_groups": None,
                "notes": "",
            }
            for row in expected_rows()
            if row["split"] == "tuning"
        ],
    }


def validate_labels(record: dict, report: dict) -> tuple[dict, dict]:
    base = template(report)
    require(set(record) == set(base), "label_root_fields")
    require(
        all(record[field] == base[field] for field in base if field != "cases"),
        "label_catalog_dataset_or_protocol_changed",
    )
    rows = record["cases"]
    require(isinstance(rows, list) and len(rows) == 40, "label_case_count")
    by_id = {row["case_id"]: row for row in rows}
    require(
        len(by_id) == 40 and set(by_id) == {r["case_id"] for r in base["cases"]},
        "label_missing_duplicate_foreign_case",
    )
    keys = {row["chunk_key"] for row in base["catalog"]}
    cases = {case["id"]: case for case in tuning_cases()}
    grades = {}
    for expected in base["cases"]:
        row = by_id[expected["case_id"]]
        require(set(row) == set(expected), "label_case_fields")
        require(
            all(
                row[field] == expected[field]
                for field in ("case_id", "split", "case_sha256")
            ),
            "label_case_binding",
        )
        require(
            row["status"] in {"pending", "approved", "changes_requested"},
            "label_status",
        )
        require(
            isinstance(row["notes"], str) and len(row["notes"]) <= 4000, "label_notes"
        )
        require(
            row["default_grade"] is None
            or type(row["default_grade"]) is int
            and row["default_grade"] == 0,
            "label_default_grade",
        )
        overrides = row["grade_overrides"]
        require(
            isinstance(overrides, list) and len(overrides) <= len(keys),
            "label_override_count",
        )
        judged = {}
        for item in overrides:
            require(
                isinstance(item, dict) and set(item) == {"chunk_key", "grade"},
                "label_override_fields",
            )
            key, grade = item["chunk_key"], item["grade"]
            require(key in keys and key not in judged, "label_override_identity")
            require(type(grade) is int and 0 <= grade <= 3, "label_grade_invalid")
            judged[key] = grade
        groups = row["entry_groups"]
        if groups is not None:
            require(
                isinstance(groups, list) and len(groups) <= len(keys),
                "entry_groups_invalid",
            )
            used = set()
            for group in groups:
                require(
                    isinstance(group, list)
                    and bool(group)
                    and all(isinstance(key, str) and key in keys for key in group)
                    and len(set(group)) == len(group)
                    and not used.intersection(group),
                    "entry_group_duplicate_or_foreign",
                )
                used.update(group)
        for field in ("full_catalog_reviewed", "protocol_accepted"):
            require(
                row[field] is None or type(row[field]) is bool, "label_boolean_invalid"
            )
        if row["status"] == "pending":
            continue
        require(
            row["actor_kind"] == "human"
            and isinstance(row["reviewer"], str)
            and bool(row["reviewer"].strip())
            and len(row["reviewer"]) <= 200,
            "label_human_required",
        )
        require(isinstance(row["reviewed_at"], str), "label_time_required")
        require(
            datetime.fromisoformat(row["reviewed_at"]).tzinfo is not None,
            "label_time_timezone_required",
        )
        if row["status"] == "changes_requested":
            require(bool(row["notes"].strip()), "label_finding_required")
            continue
        require(
            row["full_catalog_reviewed"] is True
            and row["protocol_accepted"] is True
            and row["default_grade"] == 0,
            "label_full_catalog_and_protocol_required",
        )
        require(groups is not None, "label_entry_judgment_missing")
        answerable = cases[row["case_id"]]["gold"]["expected"] == "answer"
        require(bool(groups) == answerable, "label_entry_answerability_mismatch")
        require(
            all(judged.get(key, 0) > 0 for group in groups for key in group),
            "label_entry_not_relevant",
        )
        grades[row["case_id"]] = {key: judged.get(key, 0) for key in keys}
    counts = Counter(row["status"] for row in rows)
    return {
        "stage": "human_relevance_structure_not_semantic_certification",
        "cases": 40,
        "catalog_chunks": 91,
        "pending": counts["pending"],
        "approved": counts["approved"],
        "changes_requested": counts["changes_requested"],
        "complete": counts["approved"] == 40,
        "quality_gate": "BLOCKED",
        "semantic_review_performed_by_tool": False,
    }, grades


def validate_ranking(ranking: list[str], grades: dict[str, int]) -> None:
    require(
        isinstance(ranking, list)
        and len(ranking) == len(set(ranking))
        and all(key in grades for key in ranking),
        "ranking_duplicate_or_foreign",
    )
    require(
        all(type(grade) is int and 0 <= grade <= 3 for grade in grades.values()),
        "ranking_grade_invalid",
    )


def ndcg_at_10(ranking: list[str], grades: dict[str, int]) -> float | None:
    """IDCG 用完整判斷宇宙，不只 retrieved pool；沒有正相關時為 N/A。"""
    validate_ranking(ranking, grades)

    def dcg(values):
        return sum(
            (2**value - 1) / math.log2(rank + 2)
            for rank, value in enumerate(values[:10])
        )

    ideal = dcg(sorted(grades.values(), reverse=True))
    return dcg([grades[key] for key in ranking]) / ideal if ideal else None


def entry_recall(
    ranking: list[str], groups: list[list[str]], depth: int
) -> float | None:
    """一群代表一個必要入口，群內是人工核准的等價 chunk；不計完整表格列。"""
    require(type(depth) is int and depth > 0, "recall_depth_invalid")
    require(len(ranking) == len(set(ranking)), "recall_duplicate_result")
    if not groups:
        return None
    retrieved = set(ranking[:depth])
    return sum(bool(retrieved.intersection(group)) for group in groups) / len(groups)


def rankings(report: dict) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """投影真正的兩個階段，不將 24 候選冒充 KIS Recall@30。"""
    _, lookup = source_catalog(report)
    cases = {case["id"]: case for case in tuning_cases()}
    stages, depths = {}, {}
    for name, source, depth_field in (
        ("kis_candidates_at_30", report["tuning_candidates"], "query_limit"),
        ("retrieval_candidates_actual", report["retrieval_trace"], None),
    ):
        runs = source["runs_per_case"]
        require(
            type(runs) is int
            and 1 <= runs <= 3
            and source["cases"] == 40
            and source["holdout_queries"] == 0,
            "trace_case_run_scope",
        )
        rows = source["records"]
        require(isinstance(rows, list) and len(rows) == 40 * runs, "trace_record_count")
        seen, projected, observed_depths = set(), [], set()
        for row in rows:
            case_id, run = row["case_id"], row["run"]
            require(
                case_id in cases
                and type(run) is int
                and 1 <= run <= runs
                and (case_id, run) not in seen,
                "trace_duplicate_foreign_run",
            )
            seen.add((case_id, run))
            case = cases[case_id]
            require(
                row["split"] == "tuning"
                and row["type"] == case["type"]
                and row["original_question_sha256"]
                == hashlib.sha256(case["question"].encode()).hexdigest(),
                "trace_question_binding",
            )
            depth = source[depth_field] if depth_field else row["candidate_limit"]
            require(type(depth) is int and 0 < depth <= 50, "trace_depth_invalid")
            require(not depth_field or depth == 30, "kis_recall_requires_depth_30")
            observed_depths.add(depth)
            candidates = row["candidates"]
            require(
                isinstance(candidates, list) and len(candidates) <= depth,
                "trace_candidate_count",
            )
            order = []
            mapped = {item["chunk_id"]: item for item in report["mapping"]}
            for rank, candidate in enumerate(candidates, 1):
                require(
                    type(candidate["rank"]) is int
                    and candidate["rank"] == rank
                    and candidate["chunk_id"] in lookup,
                    "trace_rank_or_identity",
                )
                if depth_field:
                    require(
                        set(candidate)
                        == MAPPING_FIELDS | {"rank", "score", "external_llm_allowed"}
                        and candidate["external_llm_allowed"] is False
                        and all(
                            candidate[field] == mapped[candidate["chunk_id"]][field]
                            for field in MAPPING_FIELDS
                        ),
                        "trace_candidate_binding",
                    )
                else:
                    require(
                        set(candidate) == {"chunk_id", "rank", "score"},
                        "trace_compact_candidate_fields",
                    )
                score = candidate["score"]
                require(
                    type(score) in {float, int} and math.isfinite(score),
                    "trace_score_nonfinite",
                )
                order.append(lookup[candidate["chunk_id"]])
            require(len(order) == len(set(order)), "trace_duplicate_chunk")
            projected.append({"case_id": case_id, "run": run, "ranking": order})
        require(
            seen == {(key, run) for key in cases for run in range(1, runs + 1)},
            "trace_missing_run",
        )
        require(len(observed_depths) == 1, "trace_depth_changed")
        stages[name], depths[name] = projected, observed_depths.pop()
    return stages, depths


def score(report: dict, labels: dict, gold_review: dict) -> dict:
    summary, grades = validate_labels(labels, report)
    gold = validate_gold(gold_review)
    require(summary["complete"] and gold["record_complete"], "human_judgments_pending")
    stages, depths = rankings(report)
    judgments = {row["case_id"]: row for row in labels["cases"]}
    cases = {case["id"]: case for case in tuning_cases()}
    result = {}
    for stage, observations in stages.items():
        measured = []
        for row in observations:
            case_id = row["case_id"]
            measured.append(
                {
                    "case_id": case_id,
                    "run": row["run"],
                    "type": cases[case_id]["type"],
                    "answerable": cases[case_id]["gold"]["expected"] == "answer",
                    "entry_recall": entry_recall(
                        row["ranking"],
                        judgments[case_id]["entry_groups"],
                        depths[stage],
                    ),
                    "ndcg_at_10": ndcg_at_10(row["ranking"], grades[case_id]),
                }
            )
        aggregates = {}
        for kind in ("all", *sorted({row["type"] for row in measured})):
            rows = [row for row in measured if kind == "all" or row["type"] == kind]
            aggregate = {"observations": len(rows)}
            for metric in ("entry_recall", "ndcg_at_10"):
                values = [row[metric] for row in rows if row[metric] is not None]
                aggregate[metric] = sum(values) / len(values) if values else None
                aggregate[metric + "_denominator"] = len(values)
                aggregate[metric + "_not_applicable"] = len(rows) - len(values)
            aggregates[kind] = aggregate
        result[stage] = {
            "candidate_depth": depths[stage],
            "recall_name": "entry_recall_at_" + str(depths[stage]),
            "runs_per_case": len(observations) // 40,
            "aggregates": aggregates,
            "records": measured,
        }
    return {
        "stage": "tuning_retrieval_metrics_against_human_judgments",
        "dataset_manifest_sha256": labels["dataset_manifest_sha256"],
        "catalog_sha256": labels["catalog_sha256"],
        "labels_sha256": digest(labels),
        "gold_review_sha256": digest(gold_review),
        "results": result,
        "holdout_queries": 0,
        "provider_calls": 0,
        "generation_quality": "NOT_MEASURED",
        "freeze": "NOT_CERTIFIED_BY_THIS_TOOL",
        "quality_gate": "BLOCKED",
        "metrics_status": "MEASURED_NOT_U0_PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("template", "check", "score"))
    parser.add_argument("--report", type=Path, default=BASELINE)
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument(
        "--gold-review", type=Path, default=ROOT / "gold-review-v2.template.json"
    )
    args = parser.parse_args()
    try:
        report = read_record(args.report)
        stages, depths = rankings(report)  # 即使 pending 也先拒絕損壞的真實 trace。
        if args.action == "template":
            result = template(report)
        elif args.action == "check":
            result, _ = validate_labels(read_record(args.labels), report)
            result["trace_observations"] = {
                key: len(rows) for key, rows in stages.items()
            }
            result["trace_candidate_depths"] = depths
        else:
            result = score(
                report, read_record(args.labels), read_record(args.gold_review)
            )
        if args.action != "template":
            result["source_report_canonical_sha256"] = digest(report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "check": "invalid_or_pending_retrieval_evaluation",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

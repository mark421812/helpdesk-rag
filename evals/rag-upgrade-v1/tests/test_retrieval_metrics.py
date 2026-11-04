"""離線數學／輸入防護單元測試；toy rankings 不是服務或品質證據。"""

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from gold_review_v2 import template as gold_template
from retrieval_metrics_v2 import (
    LABELS,
    MAPPING_FIELDS,
    digest,
    entry_recall,
    ndcg_at_10,
    rankings,
    read_record,
    score,
    source_catalog,
    template,
    tuning_cases,
    validate_labels,
)


def toy_report() -> dict:
    """只測離線欄位 validator，沒有連 PostgreSQL/HTTP 或假裝模型執行。"""
    labels = json.loads(LABELS.read_text())
    mapping = []
    for index, item in enumerate(labels["catalog"], 1):
        mapping.append(
            {
                **{
                    field: value
                    for field, value in item.items()
                    if field != "chunk_key"
                },
                "document_id": str(UUID(int=index)),
                "version_id": str(UUID(int=index + 100)),
                "chunk_id": str(UUID(int=index + 200)),
            }
        )
    kis, retrieval = [], []
    import hashlib

    for case in tuning_cases():
        basic = {
            "case_id": case["id"],
            "type": case["type"],
            "split": "tuning",
            "original_question_sha256": hashlib.sha256(
                case["question"].encode()
            ).hexdigest(),
        }
        kis.append(
            {
                **basic,
                "run": 1,
                "candidates": [
                    {**row, "rank": rank, "score": 0.5, "external_llm_allowed": False}
                    for rank, row in enumerate(mapping[:30], 1)
                ],
            }
        )
        for run in range(1, 4):
            retrieval.append(
                {
                    **basic,
                    "run": run,
                    "candidate_limit": 24,
                    "candidates": [
                        {"chunk_id": row["chunk_id"], "rank": rank, "score": 0.5}
                        for rank, row in enumerate(mapping[:24], 1)
                    ],
                }
            )
    return {
        "status": "PASSED",
        "exit_code": 0,
        "dataset_manifest_sha256": labels["dataset_manifest_sha256"],
        "company_data_touched": False,
        "external_llm_calls": 0,
        "production_files_changed": 0,
        "canonical_mismatches": 0,
        "jobs_failed": 0,
        "jobs_completed": 40,
        "postgres_major": 16,
        "cleanup": {"status": "PASSED"},
        "mapping": mapping,
        "tuning_candidates": {
            "runs_per_case": 1,
            "cases": 40,
            "holdout_queries": 0,
            "query_limit": 30,
            "records": kis,
        },
        "retrieval_trace": {
            "runs_per_case": 3,
            "cases": 40,
            "holdout_queries": 0,
            "records": retrieval,
        },
    }


def toy_approved_labels(report: dict) -> dict:
    labels = template(report)
    first_key = labels["catalog"][0]["chunk_key"]
    answerable = {
        case["id"] for case in tuning_cases() if case["gold"]["expected"] == "answer"
    }
    for row in labels["cases"]:
        row.update(
            status="approved",
            actor_kind="human",
            reviewer="toy-not-real-human-review",
            reviewed_at="2026-10-05T08:00:00+08:00",
            full_catalog_reviewed=True,
            protocol_accepted=True,
            default_grade=0,
            grade_overrides=[{"chunk_key": first_key, "grade": 3}],
            entry_groups=[[first_key]] if row["case_id"] in answerable else [],
        )
    return labels


def toy_approved_gold() -> dict:
    result = gold_template()
    for row in result["cases"]:
        row.update(
            status="approved",
            actor_kind="human",
            reviewer="toy-review-only",
            reviewed_at="2026-10-05T08:00:00+08:00",
            independent_from_implementation=True,
        )
        row["checks"] = {key: True for key in row["checks"]}
    return result


class FormalMetricMath(unittest.TestCase):
    def test_perfect_and_reversed_order(self):
        grades = {"a": 3, "b": 2, "c": 1, "d": 0}
        self.assertAlmostEqual(ndcg_at_10(["a", "b", "c", "d"], grades), 1)
        self.assertLess(ndcg_at_10(["d", "c", "b", "a"], grades), 1)

    def test_missing_best_result_reduces_score(self):
        self.assertAlmostEqual(
            ndcg_at_10(["weak"], {"weak": 1, "missing": 3}),
            1 / (7 + 1 / math.log2(3)),
        )

    def test_no_positive_judgments_are_na_not_perfect(self):
        self.assertIsNone(ndcg_at_10(["a"], {"a": 0}))
        self.assertEqual(ndcg_at_10([], {"a": 3}), 0)

    def test_rank_11_does_not_inflate_ndcg(self):
        keys = [str(index) for index in range(11)]
        grades = {key: 0 for key in keys}
        grades[keys[-1]] = 3
        self.assertEqual(ndcg_at_10(keys, grades), 0)

    def test_duplicates_foreign_keys_boolean_nan_grade_rejected(self):
        for ranking, grades in (
            (["a", "a"], {"a": 3}),
            (["foreign"], {"a": 3}),
            (["a"], {"a": True}),
            (["a"], {"a": float("nan")}),
            (["a"], {"a": 4}),
            (["a"], {"a": -1}),
        ):
            with self.assertRaises(ValueError):
                ndcg_at_10(ranking, grades)

    def test_entry_groups_allow_equivalence_but_require_each_entry(self):
        self.assertEqual(entry_recall(["b", "other"], [["a", "b"], ["c"]], 30), 0.5)
        self.assertEqual(entry_recall(["b", "c"], [["a", "b"], ["c"]], 30), 1)
        self.assertIsNone(entry_recall(["a"], [], 30))
        self.assertEqual(entry_recall(["other", "a"], [["a"]], 1), 0)
        with self.assertRaises(ValueError):
            entry_recall(["a", "a"], [["a"]], 30)


class HumanRelevanceGuards(unittest.TestCase):
    def setUp(self):
        self.report = toy_report()
        self.labels = template(self.report)

    def test_committed_template_is_exactly_pending(self):
        committed = json.loads(LABELS.read_text())
        self.assertEqual(committed, self.labels)
        result, grades = validate_labels(committed, self.report)
        self.assertEqual(result["pending"], 40)
        self.assertFalse(result["complete"])
        self.assertEqual(grades, {})
        self.assertEqual(result["quality_gate"], "BLOCKED")

    def test_catalog_stable_across_new_run_uuids(self):
        catalog, _ = source_catalog(self.report)
        other = copy.deepcopy(self.report)
        for index, row in enumerate(other["mapping"], 1):
            for offset, field in enumerate(("document_id", "version_id", "chunk_id")):
                row[field] = str(UUID(int=1000 * (offset + 1) + index))
        self.assertEqual(source_catalog(other)[0], catalog)
        other["mapping"][0]["content_sha256"] = "1" * 64
        self.assertNotEqual(source_catalog(other)[0], catalog)
        with self.assertRaises(ValueError):
            validate_labels(self.labels, other)

    def test_changed_catalog_case_hash_split_and_unknown_fields_rejected(self):
        for mode in ("catalog", "hash", "split", "unknown", "duplicate", "missing"):
            labels = copy.deepcopy(self.labels)
            if mode == "catalog":
                labels["catalog_sha256"] = "a" * 64
            elif mode == "hash":
                labels["cases"][0]["case_sha256"] = "a" * 64
            elif mode == "split":
                labels["cases"][0]["split"] = "holdout"
            elif mode == "unknown":
                labels["cases"][0]["attestation"] = "never-copy"
            elif mode == "duplicate":
                labels["cases"][1] = copy.deepcopy(labels["cases"][0])
            else:
                labels["cases"].pop()
            with self.assertRaises(ValueError):
                validate_labels(labels, self.report)

    def test_approval_requires_human_full_catalog_and_protocol(self):
        for field, value in (
            ("actor_kind", "agent"),
            ("reviewer", ""),
            ("reviewed_at", "2026-10-05T08:00:00"),
            ("full_catalog_reviewed", None),
            ("full_catalog_reviewed", 1),
            ("protocol_accepted", False),
            ("default_grade", None),
            ("default_grade", True),
            ("entry_groups", None),
        ):
            labels = toy_approved_labels(self.report)
            labels["cases"][0][field] = value
            with self.assertRaises(ValueError):
                validate_labels(labels, self.report)

    def test_grade_foreign_duplicate_nonfinite_or_boolean_rejected(self):
        for mode in ("foreign", "duplicate", "nan", "boolean", "high", "low"):
            labels = copy.deepcopy(self.labels)
            key = labels["catalog"][0]["chunk_key"]
            overrides = [{"chunk_key": key, "grade": 1}]
            if mode == "foreign":
                overrides[0]["chunk_key"] = "a" * 64
            elif mode == "duplicate":
                overrides *= 2
            else:
                overrides[0]["grade"] = {
                    "nan": float("nan"),
                    "boolean": True,
                    "high": 4,
                    "low": -1,
                }[mode]
            labels["cases"][0]["grade_overrides"] = overrides
            with self.assertRaises(ValueError):
                validate_labels(labels, self.report)

    def test_entry_groups_reject_overlap_foreign_and_irrelevant(self):
        for mode in ("overlap", "foreign", "irrelevant", "empty"):
            labels = toy_approved_labels(self.report)
            row = labels["cases"][0]
            key = labels["catalog"][0]["chunk_key"]
            row["entry_groups"] = {
                "overlap": [[key], [key]],
                "foreign": [["a" * 64]],
                "irrelevant": [[labels["catalog"][1]["chunk_key"]]],
                "empty": [],
            }[mode]
            with self.assertRaises(ValueError):
                validate_labels(labels, self.report)

    def test_refusal_not_in_entry_recall_denominator(self):
        labels = toy_approved_labels(self.report)
        refused = next(
            case["id"]
            for case in tuning_cases()
            if case["gold"]["expected"] != "answer"
        )
        row = next(row for row in labels["cases"] if row["case_id"] == refused)
        row["entry_groups"] = [[labels["catalog"][0]["chunk_key"]]]
        with self.assertRaises(ValueError):
            validate_labels(labels, self.report)

    def test_changes_requested_has_finding_and_cannot_score(self):
        labels = toy_approved_labels(self.report)
        row = labels["cases"][0]
        row.update(status="changes_requested", notes="")
        with self.assertRaises(ValueError):
            validate_labels(labels, self.report)
        row["notes"] = "Toy source finding; not a real review."
        self.assertEqual(
            validate_labels(labels, self.report)[0]["changes_requested"], 1
        )
        with self.assertRaises(ValueError):
            score(self.report, labels, toy_approved_gold())

    def test_pending_gold_or_labels_never_score(self):
        with self.assertRaises(ValueError):
            score(self.report, self.labels, toy_approved_gold())
        with self.assertRaises(ValueError):
            score(self.report, toy_approved_labels(self.report), gold_template())

    def test_all_runs_retained_and_no_overall_quality_pass(self):
        result = score(
            self.report, toy_approved_labels(self.report), toy_approved_gold()
        )
        kis, retrieval = result["results"].values()
        self.assertEqual(kis["candidate_depth"], 30)
        self.assertEqual(retrieval["candidate_depth"], 24)
        self.assertEqual(len(kis["records"]), 40)
        self.assertEqual(len(retrieval["records"]), 120)
        self.assertEqual({r["run"] for r in retrieval["records"]}, {1, 2, 3})
        self.assertLess(retrieval["aggregates"]["all"]["entry_recall_denominator"], 120)
        self.assertEqual(result["quality_gate"], "BLOCKED")
        self.assertEqual(result["generation_quality"], "NOT_MEASURED")
        self.assertEqual(result["provider_calls"], 0)

    def test_partial_trace_wrong_question_and_nonfinite_score_rejected(self):
        for mode in ("missing", "duplicate", "question", "foreign", "rank", "nan"):
            report = copy.deepcopy(self.report)
            rows = report["retrieval_trace"]["records"]
            if mode == "missing":
                rows.pop()
            elif mode == "duplicate":
                rows[1] = copy.deepcopy(rows[0])
            elif mode == "question":
                rows[0]["original_question_sha256"] = "b" * 64
            elif mode == "foreign":
                rows[0]["candidates"][0]["chunk_id"] = str(UUID(int=98765))
            elif mode == "rank":
                rows[0]["candidates"][0]["rank"] = True
            else:
                rows[0]["candidates"][0]["score"] = float("nan")
            with self.assertRaises(ValueError):
                rankings(report)

    def test_kis_depth_governance_and_mapping_binding_rejected(self):
        for mode in ("depth", "external", "content", "unknown"):
            report = copy.deepcopy(self.report)
            if mode == "depth":
                report["tuning_candidates"]["query_limit"] = 24
            else:
                item = report["tuning_candidates"]["records"][0]["candidates"][0]
                if mode == "external":
                    item["external_llm_allowed"] = True
                elif mode == "content":
                    item["content_sha256"] = "e" * 64
                else:
                    item["source_path"] = "never-print"
            with self.assertRaises(ValueError):
                rankings(report)

    def test_failed_cleanup_or_missing_mapping_rejected(self):
        for mode in ("cleanup", "failed", "missing", "duplicate", "field"):
            report = copy.deepcopy(self.report)
            if mode == "cleanup":
                report["cleanup"]["status"] = "FAILED"
            elif mode == "failed":
                report["status"] = "FAILED"
            elif mode == "missing":
                report["mapping"].pop()
            elif mode == "duplicate":
                report["mapping"][1] = copy.deepcopy(report["mapping"][0])
            else:
                report["mapping"][0]["source_url"] = "never-print"
            with self.assertRaises(ValueError):
                source_catalog(report)

    def test_output_does_not_copy_private_review_notes_or_reviewer(self):
        labels = toy_approved_labels(self.report)
        labels["cases"][0]["notes"] = "sentinel-never-log-private-notes"
        result = score(self.report, labels, toy_approved_gold())
        rendered = json.dumps(result)
        self.assertNotIn("sentinel-never-log-private-notes", rendered)
        self.assertNotIn("toy-not-real-human-review", rendered)
        self.assertNotIn("source_path", rendered)
        self.assertNotIn("attestation", rendered)
        self.assertEqual(result["labels_sha256"], digest(labels))

    def test_json_duplicate_nonfinite_and_oversized_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "toy.json"
            for raw in (
                b'{"x":1,"x":2}',
                b'{"x":NaN}',
                b'{"x":Infinity}',
                b'{"x":-Infinity}',
                b" " * 2_000_001,
            ):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    read_record(path)

    def test_mapping_allowlist_is_explicit(self):
        self.assertEqual(len(MAPPING_FIELDS), 9)
        self.assertNotIn("content", MAPPING_FIELDS)


if __name__ == "__main__":
    unittest.main()

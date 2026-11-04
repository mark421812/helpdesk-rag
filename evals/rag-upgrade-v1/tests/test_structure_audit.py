"""結構稽核工具的 guard／純文字測試；不是 PostgreSQL mock 或 RAG 品質分數。"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import structure_audit as audit
import structure_probe as probe


def sample_evidence() -> dict:
    return {
        "status": "PASSED",
        "exit_code": 0,
        "stage": "isolated_kis_persistence_not_answer_quality",
        "dataset_manifest_sha256": "a" * 64,
        "tools_sha256": {"probe.py": "b" * 64},
        "company_data_touched": False,
        "external_llm_calls": 0,
        "model_downloads": 0,
        "postgres_major": 16,
        "migration": {"exit_code": 0},
        "canonical_mismatches": 0,
        "jobs_completed": 40,
        "jobs_failed": 0,
        "documents": 40,
        "versions": 40,
        "chunks": 80,
        "vector_points": 80,
        "vector_dimension": 384,
        "cleanup": {
            "status": "PASSED",
            "unremoved_exact_ids": [],
            "created_containers": 9,
        },
        "containers": [{"log_driver": "none"} for _ in range(9)],
    }


def sample_mapping() -> tuple[list[dict], dict]:
    entry = {
        "fixture": "T01/catalog.docx",
        "chunk_index": 0,
        "document_id": str(uuid4()),
        "version_id": str(uuid4()),
        "chunk_id": str(uuid4()),
        "content_sha256": "a" * 64,
        "section_sha256": "b" * 64,
        "page_number": None,
        "token_count": 100,
    }
    return [
        {
            "fixture": entry["fixture"],
            "chunks": [
                {
                    "index": 0,
                    "content_sha256": entry["content_sha256"],
                    "section_sha256": entry["section_sha256"],
                    "page": None,
                    "tokens": 100,
                }
            ],
        }
    ], {"chunks": 1, "mapping": [entry]}


class StructureGuards(unittest.TestCase):
    def test_cell_order_is_not_bag_of_words(self) -> None:
        self.assertTrue(
            probe.ordered_cells(
                "R-T01-1\n甲\n資格甲\n100 元\n開放",
                [
                    "R-T01-1",
                    "甲",
                    "資格甲",
                    "100 元",
                    "開放",
                ],
            )
        )
        self.assertFalse(
            probe.ordered_cells(
                "甲 R-T01-1 開放 100 元",
                [
                    "R-T01-1",
                    "甲",
                    "100 元",
                    "開放",
                ],
            )
        )

    def test_cells_cannot_be_borrowed_from_next_row(self) -> None:
        rows = probe.rows_in_text(
            "R-T01-1 甲 資格甲\nR-T01-2 乙 資格乙 100 元 開放",
            "T01",
            {
                "R-T01-1": ["R-T01-1", "甲", "資格甲", "100 元", "開放"],
                "R-T01-2": ["R-T01-2", "乙", "資格乙", "100 元", "開放"],
            },
        )
        self.assertFalse(rows[0]["linear_text_cell_order"])
        self.assertTrue(rows[1]["linear_text_cell_order"])

    def test_foreign_row_is_observed_not_certified(self) -> None:
        rows = probe.rows_in_text("R-T01-99 甲 開放", "T01", {})
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["known_row"])
        self.assertFalse(rows[0]["linear_text_cell_order"])

    def test_group_and_anchor_boundaries(self) -> None:
        rows = probe.rows_in_text(
            "R-H01-1 乙\nR-T01-10 甲",
            "T01",
            {
                "R-T01-1": ["R-T01-1", "甲"],
            },
        )
        self.assertEqual([r["anchor"] for r in rows], ["R-T01-10"])
        self.assertFalse(rows[0]["known_row"])

    def test_row_observation_contains_no_text(self) -> None:
        expected = {"R-T01-1": ["R-T01-1", "自製資格", "100 元", "開放"]}
        result = probe.rows_in_text("R-T01-1 自製資格 100 元 開放", "T01", expected)
        self.assertEqual(
            set(result[0]), {"anchor", "known_row", "linear_text_cell_order"}
        )
        self.assertNotIn("自製資格", str(result))

    def test_valid_historical_evidence(self) -> None:
        audit.validate_evidence(sample_evidence(), "a" * 64, {"probe.py": "b" * 64})

    def test_stale_failed_or_incomplete_evidence_is_rejected(self) -> None:
        invalid = {
            "status": "BLOCKED",
            "exit_code": 2,
            "stage": "parser_only",
            "dataset_manifest_sha256": "c" * 64,
            "tools_sha256": {},
            "company_data_touched": True,
            "external_llm_calls": 1,
            "model_downloads": 1,
            "postgres_major": 15,
            "migration": {"exit_code": 2},
            "canonical_mismatches": 1,
            "jobs_completed": 39,
            "jobs_failed": 1,
            "documents": 39,
            "versions": 39,
            "chunks": 79,
            "vector_points": 79,
            "vector_dimension": 768,
        }
        for field, value in invalid.items():
            with self.subTest(field=field):
                evidence = sample_evidence()
                evidence[field] = value
                with self.assertRaises(audit.AuditFailure):
                    audit.validate_evidence(evidence, "a" * 64, {"probe.py": "b" * 64})

    def test_log_or_cleanup_failure_is_rejected(self) -> None:
        for field in ("log", "cleanup", "unremoved", "collection"):
            with self.subTest(field=field):
                evidence = sample_evidence()
                if field == "log":
                    evidence["containers"][0]["log_driver"] = "json-file"
                elif field == "cleanup":
                    evidence["cleanup"]["status"] = "FAILED"
                elif field == "unremoved":
                    evidence["cleanup"]["unremoved_exact_ids"] = ["c" * 64]
                else:
                    evidence["containers"].pop()
                with self.assertRaises(audit.AuditFailure):
                    audit.validate_evidence(evidence, "a" * 64, {"probe.py": "b" * 64})

    def test_matching_mapping_is_bound(self) -> None:
        docs, evidence = sample_mapping()
        bound = audit.bind_chunks(docs, evidence, {"T01/catalog.docx"})
        self.assertEqual(
            bound[0]["chunks"][0]["historical_chunk_id"],
            evidence["mapping"][0]["chunk_id"],
        )

    def test_content_section_page_or_tokens_changed(self) -> None:
        for field, value in (
            ("content_sha256", "c" * 64),
            ("section_sha256", "c" * 64),
            ("page", 2),
            ("tokens", 101),
        ):
            with self.subTest(field=field):
                docs, evidence = sample_mapping()
                docs[0]["chunks"][0][field] = value
                with self.assertRaises(audit.AuditFailure):
                    audit.bind_chunks(docs, evidence, {"T01/catalog.docx"})

    def test_duplicate_missing_foreign_or_empty_fixture(self) -> None:
        for kind in ("duplicate", "missing", "foreign", "empty"):
            docs, evidence = sample_mapping()
            if kind == "duplicate":
                docs.append(copy.deepcopy(docs[0]))
            elif kind == "missing":
                docs.clear()
            elif kind == "foreign":
                docs[0]["fixture"] = "company/secret.pdf"
            else:
                docs[0]["chunks"].clear()
            with self.subTest(kind=kind), self.assertRaises(audit.AuditFailure):
                audit.bind_chunks(docs, evidence, {"T01/catalog.docx"})

    def test_duplicate_mapping_or_uuid(self) -> None:
        for kind in ("key", "uuid"):
            docs, evidence = sample_mapping()
            second = copy.deepcopy(evidence["mapping"][0])
            if kind == "uuid":
                second["chunk_index"] = 1
            evidence["mapping"].append(second)
            evidence["chunks"] = 2
            with self.subTest(kind=kind), self.assertRaises(audit.AuditFailure):
                audit.bind_chunks(docs, evidence, {"T01/catalog.docx"})

    def test_summary_distinguishes_text_from_structure(self) -> None:
        docs = [
            {
                "fixture": "T01/catalog.docx",
                "format": ".docx",
                "block_types": {"table": 1},
                "blocks": [
                    {"rows": [{"anchor": "R-T01-1", "linear_text_cell_order": True}]}
                ],
                "expected_rows": ["R-T01-1"],
                "expected_steps": [],
                "chunks": [
                    {
                        "rows": [{"anchor": "R-T01-1", "linear_text_cell_order": True}],
                        "has_section": True,
                        "page": None,
                        "header_occurrences": 1,
                        "header_cell_order": True,
                        "steps": [],
                    }
                ],
            }
        ]
        result = audit.summarize(
            docs,
            {
                "chunk_request_fields": ["content"],
                "chunk_columns": ["content"],
                "search_result_fields": ["content"],
            },
            {
                "parsed_block_fields": ["content", "block_type"],
                "chunk_fields": ["content"],
            },
        )
        self.assertEqual(
            result["formats"][".docx"]["chunk_linear_cell_order_verified"], 1
        )
        self.assertEqual(result["parser_fields_not_in_chunk"], ["block_type"])
        self.assertTrue(
            all(
                not fields
                for fields in result["structural_names_at_chunk_boundary"].values()
            )
        )
        self.assertIn("linear_text_not_typed_rows_or_cells", result["limits"])


if __name__ == "__main__":
    unittest.main()

"""純離線 freeze guards；toy approvals 僅在記憶體／temp，非真人品質證據。"""

import copy
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_retrieval_metrics import toy_approved_gold, toy_approved_labels, toy_report

import freeze_dataset_v2 as tool
from gold_review_v2 import template as gold_template
from retrieval_metrics_v2 import digest, template, tuning_cases

STAMP = "2026-10-05T00:00:00+00:00"


class DatasetFreezeGuards(unittest.TestCase):
    def setUp(self):
        self.report = toy_report()  # 純合成 fixture，不依賴被 Git 排除的 runtime。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.baseline = Path(temporary.name) / "synthetic-report.json"
        self.baseline.write_text(json.dumps(self.report))
        # 只在本測試綁定合成 report；正式工具的固定來源 hash 與 validator 不變。
        self.enterContext(patch.object(tool, "BASELINE", self.baseline))
        self.enterContext(
            patch.object(tool, "SOURCE_CANONICAL_SHA256", digest(self.report))
        )
        self.gold = toy_approved_gold()  # 明確 toy，不寫 workspace reviews。
        self.labels = toy_approved_labels(self.report)

    def record(self):
        return tool.snapshot(self.gold, self.labels, self.report, STAMP)

    def test_enabled_snapshot_binds_all_cases_and_judgments(self):
        record = self.record()
        self.assertEqual(len(record["cases"]), 80)
        self.assertEqual(record["gold_review_canonical_sha256"], digest(self.gold))
        self.assertEqual(
            record["tuning_relevance_canonical_sha256"], digest(self.labels)
        )
        self.assertEqual(record["quality_gate"], "BLOCKED")
        self.assertEqual(record["runtime_configuration"], "NOT_FROZEN")
        self.assertIs(record["source_external_llm_allowed"], False)
        self.assertEqual(
            record["snapshot_id"],
            digest(
                {key: value for key, value in record.items() if key != "snapshot_id"}
            ),
        )
        tool.verify(record, self.gold, self.labels, self.report)

    def test_pending_gold_refuses_before_creating_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                tool.freeze(gold_template(), self.labels, self.report, root=root)
            self.assertEqual(list(root.iterdir()), [])

    def test_pending_relevance_refuses_before_creating_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                tool.freeze(self.gold, template(self.report), self.report, root=root)
            self.assertEqual(list(root.iterdir()), [])

    def test_changes_requested_cannot_freeze(self):
        self.gold["cases"][0]["status"] = "changes_requested"
        self.gold["cases"][0]["notes"] = "toy finding, not a real review"
        with self.assertRaises(ValueError):
            self.record()

    def test_holdout_requires_independent_human(self):
        row = next(row for row in self.gold["cases"] if row["split"] == "holdout")
        row["independent_from_implementation"] = False
        with self.assertRaises(ValueError):
            self.record()

    def test_missing_check_or_non_boolean_check_rejected(self):
        for value in (None, 1, False):
            with self.subTest(value=value):
                gold = copy.deepcopy(self.gold)
                first = gold["cases"][0]["checks"]
                first[next(iter(first))] = value
                with self.assertRaises(ValueError):
                    tool.snapshot(gold, self.labels, self.report, STAMP)

    def test_changed_source_report_rejected(self):
        self.report["retrieval_trace"]["records"][0]["candidates"][0]["score"] = 0.1
        with self.assertRaises(ValueError):
            self.record()

    def test_corrupt_dataset_rejected_without_output(self):
        with patch.object(tool, "validate_at", side_effect=ValueError("toy drift")):
            with self.assertRaises(ValueError):
                self.record()

    def test_changed_case_catalog_or_protocol_rejected(self):
        for field in ("case_sha256", "split"):
            with self.subTest(field=field):
                labels = copy.deepcopy(self.labels)
                labels["cases"][0][field] = "toy drift"
                with self.assertRaises(ValueError):
                    tool.snapshot(self.gold, labels, self.report, STAMP)
        self.labels["protocol"] = "toy alternate protocol"
        with self.assertRaises(ValueError):
            self.record()

    def test_time_requires_timezone_and_canonical_utc(self):
        for stamp in (None, 1, "2026-10-05", "2026-10-05T08:00:00+08:00"):
            with self.subTest(stamp=stamp), self.assertRaises((ValueError, TypeError)):
                tool.snapshot(self.gold, self.labels, self.report, stamp)

    def test_verify_rejects_tamper_extra_field_boolean_and_changed_inputs(self):
        record = self.record()
        for key, value in (
            ("quality_gate", "PASS"),
            ("secret", "toy"),
            ("semantic_review_performed_by_tool", 0),
        ):
            with self.subTest(key=key):
                bad = {**record, key: value}
                with self.assertRaises(ValueError):
                    tool.verify(bad, self.gold, self.labels, self.report)
        self.gold["cases"][0]["notes"] = "Changed toy note"
        with self.assertRaises(ValueError):
            tool.verify(record, self.gold, self.labels, self.report)

    def test_snapshot_does_not_contain_reviewers_notes_or_raw_questions(self):
        record = self.record()
        keys = set()

        def collect_keys(value):
            if isinstance(value, dict):
                keys.update(value)
                for child in value.values():
                    collect_keys(child)
            elif isinstance(value, list):
                for child in value:
                    collect_keys(child)

        collect_keys(record)
        self.assertFalse(
            keys.intersection(
                {
                    "reviewer",
                    "notes",
                    "question",
                    "answer",
                    "content",
                    "path",
                    "url",
                    "source_path",
                    "source_url",
                    "api_key",
                    "attestation",
                }
            )
        )
        raw = json.dumps(record, ensure_ascii=False)
        forbidden_values = ["toy-review-only", "toy-not-real-human-review"]
        forbidden_values.extend(case["question"] for case in tuning_cases())
        for forbidden in forbidden_values:
            self.assertNotIn(forbidden, raw)

    def test_changed_protocol_or_validator_bytes_invalidates_snapshot(self):
        record = self.record()
        changed = {**record["support_files_sha256"]}
        changed["relevance-protocol-v1.md"] = "a" * 64
        with patch.object(tool, "support_fingerprints", return_value=changed):
            with self.assertRaises(ValueError):
                tool.verify(record, self.gold, self.labels, self.report)

    def test_atomic_publish_no_overwrite_and_temp_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = tool.freeze(
                self.gold, self.labels, self.report, root=root, frozen_at=STAMP
            )
            stored = tool.read_snapshot(result["snapshot_id"], root=root)
            tool.verify(stored, self.gold, self.labels, self.report)
            target = root / "runtime/frozen" / (result["snapshot_id"] + ".json")
            original = target.read_bytes()
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                tool.freeze(
                    self.gold, self.labels, self.report, root=root, frozen_at=STAMP
                )
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_revalidation_failure_does_not_publish_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(tool, "verify", side_effect=ValueError("toy drift")):
                with self.assertRaises(ValueError):
                    tool.freeze(
                        self.gold, self.labels, self.report, root=root, frozen_at=STAMP
                    )
            self.assertEqual(list((root / "runtime/frozen").iterdir()), [])

    def test_symlink_output_directory_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            (root / "runtime").symlink_to(outside, target_is_directory=True)
            with self.assertRaises((ValueError, OSError)):
                tool.freeze(
                    self.gold, self.labels, self.report, root=root, frozen_at=STAMP
                )
            self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_ids_and_symlink_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for value in (None, "../outside", "g" * 64, 1):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    tool.read_snapshot(value, root=root)
            frozen = root / "runtime/frozen"
            frozen.mkdir(parents=True)
            (frozen / ("a" * 64 + ".json")).symlink_to(self.baseline)
            with self.assertRaises(OSError):
                tool.read_snapshot("a" * 64, root=root)

    def test_snapshot_parser_rejects_duplicate_nonfinite_root_and_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frozen = root / "runtime/frozen"
            frozen.mkdir(parents=True)
            snapshot_id = "a" * 64
            target = frozen / (snapshot_id + ".json")
            invalid = [
                "[]",
                '{"snapshot_id":"' + snapshot_id + '","snapshot_id":"duplicate"}',
                '{"snapshot_id":"' + snapshot_id + '","value":NaN}',
                '{"snapshot_id":"' + snapshot_id + '","value":Infinity}',
                '{"snapshot_id":"' + "b" * 64 + '"}',
                " " * 2_000_001,
            ]
            for raw in invalid:
                target.write_text(raw)  # 只建立工具的 temp 輸入攻擊樣本。
                with self.assertRaises(ValueError):
                    tool.read_snapshot(snapshot_id, root=root)

    def test_nonregular_snapshot_does_not_block_on_fifo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frozen = root / "runtime/frozen"
            frozen.mkdir(parents=True)
            os.mkfifo(frozen / ("a" * 64 + ".json"))
            with self.assertRaises(ValueError):
                tool.read_snapshot("a" * 64, root=root)

    def test_concurrent_publish_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def publish():
                try:
                    return tool.freeze(
                        self.gold, self.labels, self.report, root=root, frozen_at=STAMP
                    )["snapshot_id"]
                except FileExistsError:
                    return "collision_rejected"

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: publish(), range(2)))
            self.assertEqual(results.count("collision_rejected"), 1)
            files = list((root / "runtime/frozen").iterdir())
            self.assertEqual(len(files), 1)
            self.assertFalse(files[0].name.endswith(".tmp"))

    def test_default_cli_is_blocked_and_does_not_call_writer(self):
        for action in ("check", "freeze"):
            with self.subTest(action=action):
                output = io.StringIO()
                with patch.object(tool, "freeze") as writer, redirect_stdout(output):
                    self.assertEqual(tool.main([action]), 2)
                writer.assert_not_called()
                result = json.loads(output.getvalue())
                self.assertEqual(result["gold_pending"], 80)
                self.assertEqual(result["relevance_pending"], 40)
                self.assertEqual(result["freeze"], "NOT_PERFORMED")

    def test_cli_error_is_content_free(self):
        output = io.StringIO()
        with patch.object(tool, "read_record", side_effect=ValueError("toy-secret")):
            with redirect_stdout(output):
                self.assertEqual(tool.main(["check"]), 2)
        self.assertNotIn("toy-secret", output.getvalue())
        self.assertEqual(
            json.loads(output.getvalue())["check"], "invalid_dataset_freeze"
        )


if __name__ == "__main__":
    unittest.main()

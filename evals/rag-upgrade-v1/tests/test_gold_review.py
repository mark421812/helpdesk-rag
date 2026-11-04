"""Review 表單只能驗結構，不代替真人或把 pending 改 approved。"""

import copy
import unittest

from gold_review_v2 import template, validate


class HumanReviewGuards(unittest.TestCase):
    def approval(self, record, index=0):
        row = record["cases"][index]
        row.update(
            status="approved",
            reviewer="synthetic-test-human",
            actor_kind="human",
            reviewed_at="2026-10-04T15:00:00+08:00",
            independent_from_implementation=True,
        )
        row["checks"] = {key: True for key in row["checks"]}
        return row

    def test_template_is_pending_and_never_quality_pass(self):
        result = validate(template())
        self.assertEqual(result["pending"], 80)
        self.assertFalse(result["record_complete"])
        self.assertEqual(result["quality_gate"], "BLOCKED")
        self.assertFalse(result["semantic_review_performed_by_tool"])

    def test_missing_duplicate_foreign_case_or_hash_rejected(self):
        for mode in ("missing", "duplicate", "foreign", "hash"):
            r = template()
            if mode == "missing":
                r["cases"].pop()
            elif mode == "duplicate":
                r["cases"][1] = copy.deepcopy(r["cases"][0])
            elif mode == "foreign":
                r["cases"][0]["case_id"] = "foreign"
            else:
                r["cases"][0]["case_sha256"] = "a" * 64
            with self.assertRaises(ValueError):
                validate(r)

    def test_unknown_fields_and_different_dataset_rejected(self):
        r = template()
        r["attestation"] = "never-allowed"
        with self.assertRaises(ValueError):
            validate(r)
        r = template()
        r["dataset_manifest_sha256"] = "a" * 64
        with self.assertRaises(ValueError):
            validate(r)

    def test_approval_requires_human_time_and_all_checks(self):
        for field, value in (
            ("actor_kind", "agent"),
            ("reviewer", ""),
            ("reviewed_at", None),
            ("reviewed_at", "2026-10-04T15:00:00"),
            ("independent_from_implementation", 1),
        ):
            r = template()
            row = self.approval(r)
            row[field] = value
            with self.assertRaises(ValueError):
                validate(r)
        for value in (False, None, 1):
            r = template()
            row = self.approval(r)
            row["checks"]["evidence_support"] = value
            with self.assertRaises(ValueError):
                validate(r)

    def test_holdout_requires_independent_human(self):
        r = template()
        row = self.approval(r, 40)
        row["independent_from_implementation"] = False
        with self.assertRaises(ValueError):
            validate(r)

    def test_changes_requested_requires_finding(self):
        r = template()
        row = self.approval(r)
        row["status"] = "changes_requested"
        with self.assertRaises(ValueError):
            validate(r)
        row["notes"] = "Synthetic fixture source requires human correction."
        self.assertEqual(validate(r)["changes_requested"], 1)

    def test_even_complete_records_do_not_prove_quality_or_freeze(self):
        r = template()
        for index in range(80):
            self.approval(r, index)
        result = validate(r)
        self.assertTrue(result["record_complete"])
        self.assertEqual(result["quality_gate"], "BLOCKED")
        self.assertEqual(result["freeze"], "NOT_PERFORMED")


if __name__ == "__main__":
    unittest.main()

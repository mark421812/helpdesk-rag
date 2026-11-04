"""測試評估工具，不代表現有 RAG 服務或 40 項安全案例已通過。"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner


def observation(case: dict, run: int = 1) -> dict:
    anchors = [a for e in case["gold"]["evidence"] for a in e["anchors"]]
    return {
        "case_id": case["id"],
        "run": run,
        "candidates": [{"anchors": [a]} for a in anchors],
        "selected_anchors": anchors,
        "parser_gold_present": True,
        "source_complete": True,
        "claims_complete": case["gold"]["complete_required"],
        "human_correct": True,
        "citation_correct": True,
        "dependency_error": False,
        "latency_ms": 10.0,
        "human_reason": "supported",
        "provider": "fake",
        "model": "unit-test-only",
        "prompt_hash": "a" * 64,
        "policy_hash": "b" * 64,
    }


class EvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = runner.cases_for("tuning")
        cls.lookup = {c["id"]: c for c in cls.cases}
        cls.case = cls.cases[0]

    def test_dataset_hashes_and_split(self) -> None:
        result = runner.validate()
        self.assertEqual(result["business_cases"], 80)
        self.assertEqual(result["gold_human_review_pending"], 80)
        self.assertEqual(result["safety_executed"], 0)

    def test_no_cross_split_sources(self) -> None:
        tuning = {c["source_group"] for c in self.cases}
        held = {c["source_group"] for c in runner.cases_for("holdout")}
        self.assertFalse(tuning & held)

    def test_metrics_supported(self) -> None:
        metrics = runner.evidence_metrics(self.case, observation(self.case))
        self.assertEqual(metrics["anchor_recall_at30"], 1)
        self.assertTrue(metrics["selected_gold_coverage"])

    def test_duplicate_chunks_do_not_inflate_ndcg(self) -> None:
        obs = observation(self.case)
        obs["candidates"] *= 10
        self.assertLessEqual(
            runner.evidence_metrics(self.case, obs)["anchor_ndcg_at10_proxy"], 1
        )

    def test_complete_claim_cannot_use_partial_context(self) -> None:
        case = next(c for c in self.cases if c["type"] == "count")
        obs = observation(case)
        obs["selected_anchors"] = []
        self.assertFalse(runner.evidence_metrics(case, obs)["complete_claim_valid"])

    def test_classifies_each_failure_without_content(self) -> None:
        obs = observation(self.case)
        for field, value, expected in (
            ("dependency_error", True, "dependency_error"),
            ("parser_gold_present", False, "parser_loss"),
            ("human_correct", False, "generation_error"),
            ("human_correct", None, "manual_review_pending"),
        ):
            changed = copy.deepcopy(obs)
            changed[field] = value
            self.assertEqual(
                runner.classify(changed, runner.evidence_metrics(self.case, changed)),
                expected,
            )

    def test_empty_candidates_classify_recall(self) -> None:
        obs = observation(self.case)
        obs["candidates"] = []
        self.assertEqual(
            runner.classify(obs, runner.evidence_metrics(self.case, obs)), "recall_miss"
        )

    def test_selected_gap_classifies_packing(self) -> None:
        obs = observation(self.case)
        obs["selected_anchors"] = []
        self.assertEqual(
            runner.classify(obs, runner.evidence_metrics(self.case, obs)),
            "ranking_or_packing",
        )

    def test_fake_is_rejected_for_real_generation(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_observation(observation(self.case), self.lookup, True)

    def test_raw_secret_and_response_fields_are_rejected(self) -> None:
        for name in ("attestation", "answer", "content", "source_path", "api_key"):
            obs = observation(self.case)
            obs[name] = "synthetic forbidden field"
            with self.assertRaises(ValueError):
                runner.validate_observation(obs, self.lookup, False)

    def test_nonfinite_and_negative_latency_rejected(self) -> None:
        for value in (float("nan"), float("inf"), -1, True):
            obs = observation(self.case)
            obs["latency_ms"] = value
            with self.assertRaises(ValueError):
                runner.validate_observation(obs, self.lookup, False)

    def test_nonfixture_anchor_rejected(self) -> None:
        obs = observation(self.case)
        obs["selected_anchors"] = ["company-document"]
        with self.assertRaises(ValueError):
            runner.validate_observation(obs, self.lookup, False)

    def test_bool_values_strict(self) -> None:
        obs = observation(self.case)
        obs["parser_gold_present"] = 1
        with self.assertRaises(ValueError):
            runner.validate_observation(obs, self.lookup, False)

    def test_run_range_and_bool(self) -> None:
        for value in (0, 4, True, 1.5):
            with self.assertRaises(ValueError):
                runner.validate_observation(
                    observation(self.case, value), self.lookup, False
                )

    def test_changed_config_not_mixed(self) -> None:
        obs = [observation(c) for c in self.cases]
        obs[-1]["model"] = "another-model"
        with self.assertRaises(ValueError):
            runner.score(obs, "tuning", False)

    def test_missing_cases_and_duplicate_runs_rejected(self) -> None:
        for obs in (
            [observation(self.case)],
            [observation(c) for c in self.cases] + [observation(self.case)],
        ):
            with self.assertRaises(ValueError):
                runner.score(obs, "tuning", False)

    def test_live_requires_all_three_runs(self) -> None:
        obs = [observation(c) for c in self.cases]
        for o in obs:
            o["provider"] = "groq"
        with self.assertRaises(ValueError):
            runner.score(obs, "tuning", True)

    def test_holdout_report_is_aggregate_and_never_quality_pass(self) -> None:
        obs = [observation(c) for c in runner.cases_for("holdout")]
        result = runner.score(obs, "holdout", False)
        rendered = json.dumps(result)
        self.assertNotIn(obs[0]["case_id"], rendered)
        self.assertNotIn("question", rendered)
        self.assertEqual(result["runs"], 40)
        self.assertTrue(result["quality_gate"].startswith("BLOCKED"))


if __name__ == "__main__":
    unittest.main()

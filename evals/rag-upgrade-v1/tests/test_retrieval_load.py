"""負載工具的純合併／cgroup guards，不能替代 real Docker runtime。"""

import copy
import json
import unittest

from cgroup_resources_v2 import parse_cgroup, window
from isolated_kis_audit import AuditFailure
from retrieval_load_v2 import merge_runs
from retrieval_trace_v2 import TraceFailure


class LoadGuards(unittest.TestCase):
    def results(self):
        sample = {
            "stage": "installed",
            "status": "COLLECTED_NOT_QUALITY_SCORED",
            "python": "3.11",
            "machine": "x86_64",
            "settings": {"limit": 8},
            "settings_sha256": "synthetic-hash",
            "installed_modules_sha256": {},
            "cases": 40,
            "runs_per_case": 1,
            "allowed_context_count": 0,
            "answer_generation": "NOT_RUN",
            "holdout_queries": 0,
            "records": [
                {
                    "case_id": f"T{i:02}",
                    "type": "fact",
                    "run": 1,
                    "split": "tuning",
                    "original_question_sha256": "synthetic-hash",
                    "candidate_limit": 24,
                    "status": "external_generation_blocked",
                    "allowed_context_count": 0,
                    "restricted_count": 0,
                    "unselected_chunk_ids": [],
                    "exclusion_reasons": "NOT_OBSERVABLE",
                    "candidates": [],
                    "selected_evidence": [],
                    "usecase_http_wall_ms": i + 1,
                }
                for i in range(40)
            ],
        }
        return [copy.deepcopy(sample) for _ in range(3)]

    def test_all_three_runs_are_kept(self):
        result = merge_runs(self.results())
        self.assertEqual(result["total_requests"], 120)
        self.assertEqual({row["run"] for row in result["records"]}, {1, 2, 3})
        self.assertEqual(result["usecase_p95_ms"], 38)
        self.assertEqual(result["answer_generation"], "NOT_RUN")

    def test_missing_lane_is_not_success(self):
        with self.assertRaises(TraceFailure):
            merge_runs(self.results()[:2])

    def test_changed_snapshot_is_not_comparable(self):
        for key, value in (
            ("settings", {"limit": 30}),
            ("allowed_context_count", 1),
            ("answer_generation", "RUN"),
            ("holdout_queries", 1),
        ):
            values = self.results()
            values[1][key] = value
            with self.assertRaises(TraceFailure):
                merge_runs(values)

    def test_missing_duplicate_or_foreign_case_is_rejected(self):
        for kind in ("missing", "duplicate", "foreign"):
            values = self.results()
            if kind == "missing":
                values[1]["records"].pop()
            elif kind == "duplicate":
                values[1]["records"][1] = values[1]["records"][0]
            else:
                values[1]["records"][0]["case_id"] = "H01"
            with self.assertRaises(TraceFailure):
                merge_runs(values)

    def test_nonfinite_timing_is_not_accepted(self):
        values = self.results()
        values[2]["records"][0]["usecase_http_wall_ms"] = float("nan")
        with self.assertRaises(TraceFailure):
            merge_runs(values)

    def test_compact_projection_is_bounded_and_does_not_keep_raw_fields(self):
        values = self.results()
        for result in values:
            for row in result["records"]:
                row["candidates"] = [
                    {
                        "chunk_id": str(i).zfill(36),
                        "rank": i + 1,
                        "score": 0.8,
                        "content": "synthetic-ignored" * 500,
                    }
                    for i in range(24)
                ]
                row["answer"] = "synthetic-ignored"
        text = json.dumps(merge_runs(values))
        self.assertLess(len(text.encode()), 2_000_000)
        self.assertNotIn('"content"', text)
        self.assertNotIn('"answer"', text)


class CgroupGuards(unittest.TestCase):
    def sample(self):
        return parse_cgroup(
            "100\n200\nusage_usec 400000\nuser_usec 300000\nsystem_usec 100000\n"
            "nr_periods 10\nnr_throttled 2\nthrottled_usec 2000\n"
        )

    def test_snapshot_is_numeric_allowlist(self):
        result = self.sample()
        self.assertEqual(result["memory_lifetime_peak_bytes"], 200)
        self.assertNotIn("user_usec", result["cpu"])

    def test_invalid_or_missing_counters_fail_closed(self):
        for raw in (
            "",
            "100\n50\nusage_usec 1",
            "-1\n200\nusage_usec 1",
            "100\n200\nusage_usec -1",
            "100\n200\nusage_usec NaN",
            "100\n200\nusage_usec 1\nusage_usec 2",
        ):
            with self.assertRaises(AuditFailure):
                parse_cgroup(raw)

    def test_window_preserves_peak_scope_and_cpu_units(self):
        a = {"api": self.sample()}
        b = copy.deepcopy(a)
        b["api"]["cpu"]["usage_usec"] += 1_000_000
        result = window(a, b, 2)
        self.assertEqual(result["servers"]["api"]["average_cpu_cores"], 0.5)
        self.assertIn("lifetime", result["memory_peak_scope"])

    def test_counter_reset_is_rejected(self):
        a = {"api": self.sample()}
        b = copy.deepcopy(a)
        b["api"]["cpu"]["usage_usec"] -= 1
        with self.assertRaises(AuditFailure):
            window(a, b, 2)

    def test_missing_roles_or_invalid_elapsed_are_rejected(self):
        a = {"api": self.sample()}
        for elapsed in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(AuditFailure):
                window(a, a, elapsed)
        with self.assertRaises(AuditFailure):
            window(a, {}, 2)


if __name__ == "__main__":
    unittest.main()

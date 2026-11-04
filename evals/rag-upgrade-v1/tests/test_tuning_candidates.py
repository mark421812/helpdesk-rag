"""Pure candidate projection/latency guards，不 mock PostgreSQL 或偽造業務品質。"""

import copy
import unittest

import httpx

from kis_persistence_probe import ProbeFailure
from tuning_candidates_v2 import percentile, private_kis_url, project_hit


class CandidateGuards(unittest.TestCase):
    def test_private_endpoint_compares_parsed_fields(self):
        for url in ("http://kis-api:8080", "http://kis-api:8080/"):
            self.assertTrue(private_kis_url(httpx.URL(url)))
        client = httpx.AsyncClient(base_url="http://kis-api:8080")
        self.assertTrue(private_kis_url(client.base_url))
        for url in (
            "https://kis-api:8080",
            "http://knowledge-index-api:8080",
            "http://127.0.0.1:8080",
            "http://kis-api:8081",
            "http://kis-api:8080/other",
            "http://user:secret@kis-api:8080",
            "http://kis-api:8080?x=1",
            "http://kis-api:8080#x",
            "http://kis-api:8080.evil/",
        ):
            with self.subTest(url=url):
                try:
                    parsed = httpx.URL(url)
                except httpx.InvalidURL:
                    continue  # 被 URL parser 拒絕，不是 skip 測試。
                self.assertFalse(private_kis_url(parsed))

    def sample(self):
        entry = {
            "fixture": "T01/rules.md",
            "document_id": "doc",
            "version_id": "ver",
            "chunk_id": "chunk",
            "chunk_index": 0,
            "page_number": None,
            "token_count": 10,
            "content_sha256": "a" * 64,
            "section_sha256": "b" * 64,
        }
        chunk = {
            "content": "E-T01-F synthetic only",
            "page_number": None,
            "section_path": "section",
            "token_count": 10,
        }
        body = {"custom_metadata": {"synthetic": True}, "chunks": [chunk]}
        hit = {
            **chunk,
            "document_id": "doc",
            "version_id": "ver",
            "chunk_id": "chunk",
            "custom_metadata": body["custom_metadata"],
            "external_llm_allowed": False,
            "score": 0.8,
            "source_path": "/synthetic/never-emitted",
            "source_url": None,
        }
        return hit, {"chunk": entry}, {"T01/rules.md": body}

    def test_projection_is_content_and_path_free(self):
        hit, mapping, bodies = self.sample()
        projected = project_hit(hit, mapping, bodies)
        self.assertEqual(projected["content_sha256"], "a" * 64)
        for field in (
            "content",
            "source_path",
            "source_url",
            "section_path",
            "custom_metadata",
        ):
            self.assertNotIn(field, projected)

    def test_foreign_chunk_is_rejected(self):
        hit, mapping, bodies = self.sample()
        with self.assertRaises(ProbeFailure):
            project_hit({**hit, "chunk_id": "foreign"}, mapping, bodies)

    def test_mismatched_canonical_fields_are_rejected(self):
        hit, mapping, bodies = self.sample()
        for field, value in (
            ("document_id", "other"),
            ("version_id", "other"),
            ("content", "different synthetic"),
            ("page_number", 99),
            ("section_path", "other"),
            ("token_count", 11),
        ):
            with self.subTest(field=field), self.assertRaises(ProbeFailure):
                project_hit({**hit, field: value}, mapping, bodies)

    def test_governance_and_metadata_are_strict(self):
        hit, mapping, bodies = self.sample()
        for flag in (True, 0, None, "false"):
            with self.assertRaises(ProbeFailure):
                project_hit({**hit, "external_llm_allowed": flag}, mapping, bodies)
        changed = copy.deepcopy(hit)
        changed["custom_metadata"]["synthetic"] = False
        with self.assertRaises(ProbeFailure):
            project_hit(changed, mapping, bodies)

    def test_nonfinite_score_is_rejected(self):
        hit, mapping, bodies = self.sample()
        for score in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(ProbeFailure):
                project_hit({**hit, "score": score}, mapping, bodies)

    def test_nearest_rank_and_invalid_latency(self):
        self.assertEqual(percentile(list(range(1, 41)), 0.5), 20)
        self.assertEqual(percentile(list(range(1, 41)), 0.95), 38)
        for values in ([], [-1], [float("nan")], [float("inf")]):
            with self.assertRaises(ProbeFailure):
                percentile(values, 0.95)
        for fraction in (0, 2, float("nan"), float("inf")):
            with self.assertRaises(ProbeFailure):
                percentile([1], fraction)


if __name__ == "__main__":
    unittest.main()

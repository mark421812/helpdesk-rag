"""Pure trace projection guards；實際 Retrieval/KIS 由隔離 Docker target 驗證。"""

import copy
import unittest
from types import SimpleNamespace

from retrieval_trace_v2 import (
    POLICY_FIELDS,
    TraceFailure,
    digest,
    percentile,
    project,
    selected_projection,
    wait_ready,
)


class TraceGuards(unittest.TestCase):
    def sample(self):
        c = SimpleNamespace(
            chunk_id="chunk",
            document_id="doc",
            version_id="ver",
            content="synthetic-only",
            title="U0 synthetic",
            document_type="policy",
            section_path=None,
            page_number=1,
            token_count=15,
            chunk_index=0,
            language="zh-TW",
            score=0.8,
            confidentiality=SimpleNamespace(value="internal"),
            external_llm_allowed=False,
        )
        m = {
            "fixture": "T01/procedure.pdf",
            "chunk_id": c.chunk_id,
            "document_id": c.document_id,
            "version_id": c.version_id,
            "chunk_index": 0,
            "content_sha256": digest(c.content),
            "section_sha256": digest(""),
            "page_number": 1,
            "token_count": 15,
        }
        b = {
            "title": c.title,
            "document_type": c.document_type,
            "language": c.language,
            "confidentiality": "internal",
            "chunks": [{"content": c.content, "section_path": None, "page_number": 1}],
        }
        return c, {c.chunk_id: m}, {m["fixture"]: b}

    def test_projection_contains_no_content_or_secret(self):
        c, m, b = self.sample()
        p = project(c, m, b, rank=1)
        self.assertEqual(p["content_sha256"], digest(c.content))
        for key in (
            "content",
            "title",
            "section_path",
            "source_path",
            "source_url",
            "api_key",
            "attestation",
        ):
            self.assertNotIn(key, p)

    def test_unknown_chunk_is_rejected(self):
        c, m, b = self.sample()
        c.chunk_id = "foreign"
        with self.assertRaises(TraceFailure):
            project(c, m, b, rank=1)

    def test_changed_identity_content_and_citation_are_rejected(self):
        for field, value in (
            ("document_id", "other"),
            ("version_id", "other"),
            ("content", "other"),
            ("title", "other"),
            ("section_path", "other"),
            ("page_number", 9),
            ("token_count", 16),
            ("chunk_index", 1),
            ("language", "en"),
        ):
            with self.subTest(field=field):
                c, m, b = self.sample()
                setattr(c, field, value)
                with self.assertRaises(TraceFailure):
                    project(c, m, b, rank=1)

    def test_governance_must_be_false_boolean(self):
        for flag in (True, 0, None, "false"):
            c, m, b = self.sample()
            c.external_llm_allowed = flag
            with self.assertRaises(TraceFailure):
                project(c, m, b, rank=1)

    def test_nonfinite_scores_are_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            c, m, b = self.sample()
            c.score = value
            with self.assertRaises(TraceFailure):
                project(c, m, b, rank=1)

    def test_rank_requires_positive_integer(self):
        c, m, b = self.sample()
        for rank in (True, 0, -1, 1.2):
            with self.assertRaises(TraceFailure):
                project(c, m, b, rank=rank)

    def selected_sample(self):
        c, m, b = self.sample()
        p = project(c, m, b, rank=3)
        evidence = copy.copy(c)
        evidence.rank = 1
        evidence.citation = SimpleNamespace(
            title=c.title, section_path=c.section_path, page_number=c.page_number
        )
        evidence.governance = SimpleNamespace(
            external_llm_allowed=False,
            confidentiality=c.confidentiality,
            language=c.language,
        )
        return evidence, {c.chunk_id: (c, p)}

    def test_selected_projection_is_bound_and_content_free(self):
        e, candidates = self.selected_sample()
        p = selected_projection(e, candidates)
        self.assertEqual(p["rank"], 1)
        self.assertEqual(p["channel"], "restricted_evidence")
        self.assertNotIn("content", p)

    def test_selected_cannot_introduce_new_or_changed_evidence(self):
        e, candidates = self.selected_sample()
        with self.assertRaises(TraceFailure):
            selected_projection(e, {})
        e.content = "different"
        with self.assertRaises(TraceFailure):
            selected_projection(e, candidates)

    def test_selected_cannot_change_governance_or_citation(self):
        for field in ("governance", "citation"):
            e, candidates = self.selected_sample()
            if field == "governance":
                e.governance.external_llm_allowed = True
            else:
                e.citation.page_number = 99
            with self.assertRaises(TraceFailure):
                selected_projection(e, candidates)

    def test_policy_snapshot_has_no_endpoints_or_secrets(self):
        self.assertEqual(len(POLICY_FIELDS), len(set(POLICY_FIELDS)))
        self.assertFalse(
            any("api_key" in k or "url" in k or "path" in k for k in POLICY_FIELDS)
        )

    def test_latency_is_finite_nearest_rank(self):
        self.assertEqual(percentile(list(range(1, 41)), 0.95), 38)
        for values in ([], [-1], [float("nan")], [float("inf")]):
            with self.assertRaises(TraceFailure):
                percentile(values, 0.5)


class ReadinessGuards(unittest.IsolatedAsyncioTestCase):
    async def test_ready_does_not_repeat_query(self):
        class Probe:
            calls = 0

            async def is_ready(self):
                self.calls += 1
                return True

        probe = Probe()
        await wait_ready(probe, timeout_seconds=0.05)
        self.assertEqual(probe.calls, 1)

    async def test_unready_is_bounded_and_fail_closed(self):
        class Probe:
            async def is_ready(self):
                return False

        with self.assertRaises(TraceFailure) as failure:
            await wait_ready(Probe(), timeout_seconds=0.02)
        self.assertEqual(failure.exception.code, "trace_private_kis_not_ready")


if __name__ == "__main__":
    unittest.main()

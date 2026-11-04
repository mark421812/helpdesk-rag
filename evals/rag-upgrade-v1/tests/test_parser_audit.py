"""只測 U0 工具安全與失敗判定，不冒充真 tokenizer 或 Docker integration。"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit_parsers
import docker_parser_audit


def sample() -> dict:
    item = {
        "missing_gold_anchors": [],
        "chunks": {
            "count": 1,
            "max_tokens": 200,
            "over_limit_count": 0,
            "recount_mismatch_count": 0,
            "missing_gold_anchors": [],
        },
    }
    return {
        "documents": 40,
        "results": [copy.deepcopy(item) for _ in range(40)],
        "chunking_executed": True,
        "chunking_config": {"max_tokens": 450},
        "model_max_length": 512,
    }


def metadata() -> dict:
    return {
        "image_id": "sha256:" + "a" * 64,
        "user": "app",
        "mounts": [
            {
                "Destination": "/var/cache/huggingface",
                "Type": "volume",
                "Name": "ingestion-service_tokenizer-cache",
            },
            {
                "Destination": "/data/documents",
                "Type": "bind",
                "Source": "/company/documents",
            },
        ],
    }


class ParserAuditTests(unittest.TestCase):
    def test_complete_sample(self) -> None:
        self.assertEqual(audit_parsers.failures(sample()), [])

    def test_no_missing_parser_anchors_can_pass(self) -> None:
        report = sample()
        report["results"][0]["missing_gold_anchors"] = ["synthetic-anchor"]
        self.assertIn("parser_anchor_missing", audit_parsers.failures(report))

    def test_no_missing_chunk_anchors_can_pass(self) -> None:
        report = sample()
        report["results"][0]["chunks"]["missing_gold_anchors"] = ["synthetic-anchor"]
        self.assertIn("chunk_anchor_missing", audit_parsers.failures(report))

    def test_empty_chunks_fail(self) -> None:
        for chunks in (None, {"count": 0}):
            report = sample()
            report["results"][0]["chunks"] = chunks
            self.assertIn("empty_chunks", audit_parsers.failures(report))

    def test_token_limits_and_recount_fail(self) -> None:
        for field, value, reason in (
            ("max_tokens", 451, "chunk_token_limit"),
            ("max_tokens", 513, "chunk_token_limit"),
            ("max_tokens", 0, "chunk_token_limit"),
            ("over_limit_count", 1, "chunk_token_limit"),
            ("recount_mismatch_count", 1, "token_recount_mismatch"),
        ):
            report = sample()
            report["results"][0]["chunks"][field] = value
            self.assertIn(reason, audit_parsers.failures(report))

    def test_partial_collection_fails(self) -> None:
        report = sample()
        report["results"].pop()
        self.assertIn("document_count_mismatch", audit_parsers.failures(report))

    def test_container_has_no_network_secrets_or_company_mount(self) -> None:
        args = docker_parser_audit.command(metadata())
        for flag in (
            "--network=none",
            "--read-only",
            "--pull=never",
            "--cap-drop=ALL",
            "--no-healthcheck",
            "--installed-parser-only",
            "--no-payloads",
        ):
            self.assertIn(flag, args)
        mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(m.endswith(",readonly") for m in mounts))
        rendered = " ".join(args)
        for forbidden in (
            "/company",
            "DATABASE_URL",
            "API_KEY",
            "--env-file",
            "--privileged",
            "latest",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_non_root_and_immutable_image_required(self) -> None:
        for key, value in (
            ("user", "root"),
            ("user", "0:100"),
            ("user", ""),
            ("image_id", "helpdesk/ingestion-service:local"),
        ):
            data = metadata()
            data[key] = value
            with self.assertRaises(ValueError):
                docker_parser_audit.command(data)

    def test_missing_or_duplicate_cache_rejected(self) -> None:
        data = metadata()
        for mounts in ([], [data["mounts"][0]] * 2):
            data["mounts"] = mounts
            with self.assertRaises(ValueError):
                docker_parser_audit.command(data)


if __name__ == "__main__":
    unittest.main()

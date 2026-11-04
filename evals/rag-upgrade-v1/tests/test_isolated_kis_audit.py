"""純工具 guard tests；不 mock PostgreSQL，不冒充 persistence/runtime 驗收。"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import isolated_kis_audit as audit


class IsolationTests(unittest.TestCase):
    def test_private_resource_flags(self) -> None:
        args = audit.base_flags("a" * 32, "api", 1280, "b" * 64)
        for flag in (
            "--pull=never",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--restart=no",
            "--no-healthcheck",
            "--log-driver=none",
        ):
            self.assertIn(flag, args)
        for forbidden in (
            "--publish",
            "-p",
            "--privileged",
            "--env-file",
            "--volumes-from",
        ):
            self.assertNotIn(forbidden, args)

    def test_exact_network_required(self) -> None:
        for network in ("helpdesk-rag", "host", "bridge", "", "ab"):
            with self.assertRaises(audit.AuditFailure):
                audit.base_flags("a" * 32, "api", 1280, network)
        self.assertIn("none", audit.base_flags("a" * 32, "parser", 512, "none"))

    def test_invalid_run_identity(self) -> None:
        for run_id, role in (("../../production", "api"), ("a" * 32, "api;rm")):
            with self.assertRaises(audit.AuditFailure):
                audit.base_flags(run_id, role, 1280, "b" * 64)

    def test_cleanup_ownership_is_label_not_name(self) -> None:
        own = {"labels": {audit.LABEL: "a" * 32}}
        self.assertTrue(audit.owned(own, "a" * 32))
        for metadata in (
            {},
            {"name": "rag-u0-test"},
            {"labels": {audit.LABEL: "b" * 32}},
        ):
            self.assertFalse(audit.owned(metadata, "a" * 32))
        changed = copy.deepcopy(own)
        changed["labels"][audit.LABEL] = "a" * 31
        self.assertFalse(audit.owned(changed, "a" * 32))

    def test_generated_settings_never_reuse_production_key(self) -> None:
        first = audit.environment("a" * 32)
        second = audit.environment("a" * 32)
        self.assertNotEqual(first["API_INTERNAL_KEY"], second["API_INTERNAL_KEY"])
        self.assertNotEqual(first["QDRANT_API_KEY"], second["QDRANT_API_KEY"])
        self.assertNotEqual(first["DATABASE_URL"], second["DATABASE_URL"])
        self.assertTrue(first["DATABASE_URL"].endswith("@postgres:5432/rag_u0"))
        self.assertEqual(first["QDRANT_COLLECTION"], "rag_u0_fixture")
        for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            self.assertEqual(first[key], "1")
        self.assertFalse(any("groq" in key.lower() for key in first))

    def test_only_readonly_cache_and_eval_mounts(self) -> None:
        args = audit.mounted_tools() + audit.cache_mount(
            {
                "mounts": [
                    {
                        "Type": "volume",
                        "Destination": audit.CACHE_PATH,
                        "Name": "synthetic-cache",
                    }
                ]
            }
        )
        mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(m.endswith(",readonly") for m in mounts))
        self.assertFalse(any("/documents" in m or ".env" in m for m in mounts))

    def test_stop_never_accepts_foreign_or_short_id(self) -> None:
        resources = audit.Resources("a" * 32)
        for container_id in ("knowledge-index-service-api-1", "b" * 64, "b" * 12):
            with self.assertRaises(audit.AuditFailure):
                resources.stop(container_id)


if __name__ == "__main__":
    unittest.main()

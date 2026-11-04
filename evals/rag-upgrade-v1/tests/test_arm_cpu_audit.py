"""ARM 工具的純 guard tests；不模擬 runtime PASS 或取代真正容器 probe。"""

import copy
import hashlib
import json
import unittest

from arm_cpu_audit import (
    MODULES,
    WORKSPACE,
    decode_startup_events,
    validate_image,
    verify_snapshot,
)
from arm_cpu_probe import STARTUP_STAGES
from audit_parsers import MODEL_ID, MODEL_REVISION
from isolated_kis_audit import AuditFailure, base_flags


def example_snapshot(mode: str) -> dict:
    hashes = {}
    for name in MODULES[mode]:
        package = name.split(".")[0]
        path = (
            WORKSPACE
            / package.replace("_", "-")
            / "src"
            / (name.replace(".", "/") + ".py")
        )
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "status": "PASSED",
        "mode": mode,
        "machine": "aarch64",
        "python": "3.11.15",
        "installed_modules_sha256": hashes,
        "process_peak_rss_bytes": 1024,
        "dimension": 384,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "device": "cpu",
        "cuda_version": None,
        "vector_count": 2,
        "vectors_finite": True,
        "token_over_limit_rejected": True,
        "vector_norms": [1.0, 1.0],
    }


class ArmCompatibilityGuards(unittest.TestCase):
    def test_startup_events_allow_only_valid_ordered_prefix(self):
        events = [
            {"diagnostic_stage": stage, "elapsed_seconds": index}
            for index, stage in enumerate(STARTUP_STAGES)
        ]
        events[1].update(
            installed_modules_sha256=example_snapshot("ingestion")[
                "installed_modules_sha256"
            ],
            machine="aarch64",
            python="3.11.15",
        )
        events[-1].update(model_max_length=512, synthetic_tokens=15)
        for count in range(1, len(events) + 1):
            raw = "\n".join(json.dumps(row) for row in events[:count])
            self.assertEqual(decode_startup_events(raw), events[:count])
            self.assertEqual(decode_startup_events(raw.encode()), events[:count])

    def test_startup_output_rejects_body_unknown_fields_and_wrong_order(self):
        good = {"diagnostic_stage": "python_ready", "elapsed_seconds": 0}
        for raw in (
            None,
            "",
            "x" * 65_537,
            json.dumps({**good, "content": "synthetic sentinel"}),
            json.dumps({**good, "diagnostic_stage": "tokenizer_load_started"}),
            json.dumps(good) + "\n" + json.dumps(good),
            json.dumps({**good, "elapsed_seconds": True}),
            json.dumps({**good, "elapsed_seconds": -1}),
            json.dumps({**good, "elapsed_seconds": float("inf")}),
            "[]",
        ):
            with self.assertRaises(AuditFailure):
                decode_startup_events(raw)

    def test_startup_source_drift_is_not_diagnostic_evidence(self):
        start = {"diagnostic_stage": "python_ready", "elapsed_seconds": 0}
        source = {
            "diagnostic_stage": "source_hashes_ready",
            "elapsed_seconds": 1,
            "installed_modules_sha256": {
                name: "a" * 64 for name in MODULES["ingestion"]
            },
            "machine": "aarch64",
            "python": "3.11.15",
        }
        with self.assertRaises(AuditFailure):
            decode_startup_events(json.dumps(start) + "\n" + json.dumps(source))

    def test_image_requires_arm_linux_nonroot_immutable_id(self):
        original = {
            "image_id": "sha256:" + "a" * 64,
            "os": "linux",
            "architecture": "arm64",
            "user": "app",
        }
        validate_image(original)
        for field, value in (
            ("image_id", "latest"),
            ("os", "windows"),
            ("architecture", "amd64"),
            ("user", "root"),
            ("user", "0:1000"),
            ("user", ""),
        ):
            changed = {**original, field: value}
            with self.assertRaises(AuditFailure):
                validate_image(changed)

    def test_base_flags_preserve_offline_bounded_private_resources(self):
        flags = base_flags("a" * 32, "arm-embedding", 1536, "none")
        self.assertIn("--pull=never", flags)
        self.assertIn("--log-driver=none", flags)
        self.assertIn("--read-only", flags)
        self.assertIn("--cap-drop=ALL", flags)
        self.assertEqual(flags[flags.index("--network") + 1], "none")
        self.assertIn("--memory=1536m", flags)
        self.assertNotIn("--privileged", flags)

    def test_unmatched_arch_or_python_not_current_arm_proof(self):
        original = example_snapshot("embedding")
        for field, value in (
            ("machine", "x86_64"),
            ("python", "3.13.9"),
            ("mode", "ingestion"),
            ("status", "BLOCKED"),
        ):
            changed = {**original, field: value}
            with self.assertRaises(AuditFailure):
                verify_snapshot("embedding", changed, {})

    def test_installed_modules_must_match_current_source(self):
        original = example_snapshot("embedding")
        for mode in ("hash", "missing", "extra"):
            changed = copy.deepcopy(original)
            hashes = changed["installed_modules_sha256"]
            if mode == "hash":
                hashes[next(iter(hashes))] = "b" * 64
            elif mode == "missing":
                hashes.pop(next(iter(hashes)))
            else:
                hashes["unexpected"] = "c" * 64
            with self.assertRaises(AuditFailure):
                verify_snapshot("embedding", changed, {})

    def test_cuda_wrong_model_or_missing_token_rejection_not_cpu_proof(self):
        original = example_snapshot("embedding")
        verify_snapshot("embedding", original, {})
        for field, value in (
            ("cuda_version", "12"),
            ("dimension", 768),
            ("model_revision", "latest"),
            ("model_id", "other"),
            ("device", "cuda"),
            ("vector_count", 1),
            ("vectors_finite", 1),
            ("token_over_limit_rejected", False),
        ):
            with self.assertRaises(AuditFailure):
                verify_snapshot("embedding", {**original, field: value}, {})

    def test_norm_and_rss_must_be_valid(self):
        original = example_snapshot("embedding")
        for field, value in (
            ("vector_norms", [float("nan"), 1]),
            ("vector_norms", [True, 1]),
            ("vector_norms", [0.9, 1]),
            ("vector_norms", [1]),
            ("process_peak_rss_bytes", True),
            ("process_peak_rss_bytes", 0),
        ):
            with self.assertRaises(AuditFailure):
                verify_snapshot("embedding", {**original, field: value}, {})

    def test_parser_parity_requires_all_chunk_bytes_pages_tokens(self):
        snapshot = example_snapshot("ingestion")
        mapping = [
            {
                "fixture": "T01/example.pdf",
                "chunk_index": i,
                "page_number": 1,
                "token_count": 20,
                "content_sha256": str(i).zfill(64),
                "section_sha256": "a" * 64,
            }
            for i in range(91)
        ]
        baseline = {"mapping": mapping, "dataset_manifest_sha256": "b" * 64}
        snapshot.update(
            documents=40,
            chunks=91,
            records=copy.deepcopy(mapping),
            missing_anchor_count=0,
            dataset_manifest_sha256="b" * 64,
        )
        verify_snapshot("ingestion", snapshot, baseline)
        for field, value in (
            ("content_sha256", "c" * 64),
            ("token_count", 21),
            ("page_number", 2),
            ("chunk_index", 92),
            ("section_sha256", "d" * 64),
        ):
            changed = copy.deepcopy(snapshot)
            changed["records"][0][field] = value
            with self.assertRaises(AuditFailure):
                verify_snapshot("ingestion", changed, baseline)
        for field, value in (
            ("documents", 39),
            ("chunks", 90),
            ("missing_anchor_count", 1),
            ("dataset_manifest_sha256", "e" * 64),
        ):
            with self.assertRaises(AuditFailure):
                verify_snapshot("ingestion", {**snapshot, field: value}, baseline)


if __name__ == "__main__":
    unittest.main()

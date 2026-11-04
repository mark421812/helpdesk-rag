"""v2 invocation 與 source/anchor binding guards；不是假 PostgreSQL integration。"""

import copy
import unittest

from audit_revision_v2 import row_cells_present
from build_revision_v2 import physical_rows, scenarios, values
from docker_parser_audit import CONTAINER_ROOT
from isolated_kis_audit import AuditFailure, Resources
from isolated_revision_v2 import RevisionResources, invocation_for
from kis_persistence_probe import ProbeFailure
from revision_v2_payload import observe_chunks
from revision_v2_probe import bind_anchors


class RevisionPersistenceTests(unittest.TestCase):
    def test_invocation_is_explicit_not_arbitrary_rewrite(self):
        self.assertEqual(
            invocation_for("parser", [f"{CONTAINER_ROOT}/index_fixtures.py"]),
            [f"{CONTAINER_ROOT}/revision_v2_payload.py"],
        )
        for mode in ("submit", "wait", "verify"):
            self.assertEqual(
                invocation_for(
                    mode, [f"{CONTAINER_ROOT}/kis_persistence_probe.py", mode]
                ),
                [f"{CONTAINER_ROOT}/revision_v2_probe.py", mode],
            )
        with self.assertRaises(AuditFailure):
            invocation_for("parser", ["/company/secret.py"])
        with self.assertRaises(AuditFailure):
            invocation_for(
                "verify", [f"{CONTAINER_ROOT}/kis_persistence_probe.py", "submit"]
            )

    def test_resources_preserve_original_security_methods(self):
        for method in ("inspect", "start", "stop", "cleanup", "run"):
            self.assertIs(
                getattr(RevisionResources, method), getattr(Resources, method)
            )

    def test_other_roles_are_not_changed(self):
        for role, invocation in (
            ("migration", ["alembic", "upgrade", "head"]),
            ("worker", ["python", "-m", "knowledge_index_service.worker"]),
        ):
            self.assertEqual(invocation_for(role, invocation), invocation)

    def test_one_chunk_rows_and_pages_are_observed_without_text(self):
        row = physical_rows(scenarios()[0])[0]
        content = " ".join(values(row))
        self.assertTrue(row_cells_present(content, row))
        doc = {
            "id": "T01/procedure.pdf",
            "path": "corpus/tuning/T01/procedure.pdf",
            "sha256": "a" * 64,
        }
        report = observe_chunks(
            doc, [{"content": content, "page_number": row["page"]}], [row], []
        )
        self.assertEqual(report["rows_with_cells_in_one_chunk"], 1)
        self.assertEqual(report["wrong_pdf_row_pages"], [])
        self.assertNotIn("content", report)
        self.assertEqual(report["anchors"], {row["anchor"]: [0]})

    def test_split_row_is_not_certified_as_single_chunk(self):
        row = physical_rows(scenarios()[0])[0]
        doc = {"id": "T01/catalog.docx", "path": "catalog.docx", "sha256": "a" * 64}
        report = observe_chunks(
            doc,
            [
                {"content": " ".join(values(row)[:4]), "page_number": None},
                {"content": " ".join(values(row)[4:]), "page_number": None},
            ],
            [row],
            [],
        )
        self.assertEqual(report["rows_with_cells_in_one_chunk"], 0)
        self.assertEqual(report["missing_anchors"], [])

    def test_anchor_mapping_rejects_missing_duplicate_or_changed_bindings(self):
        fixture = "T01/rules.md"
        bundle = {
            "fixtures": [
                {"fixture": fixture, "body": {"chunks": [{"content": "E-T01-F"}]}}
            ],
            "chunk_observations": [{"fixture": fixture, "anchors": {"E-T01-F": [0]}}],
        }
        mapping = [{"fixture": fixture, "chunk_index": 0, "chunk_id": "opaque-test-id"}]
        self.assertEqual(
            bind_anchors(bundle, mapping)[0]["chunk_ids"], ["opaque-test-id"]
        )
        for broken in ([], mapping + mapping):
            with self.assertRaises(ProbeFailure):
                bind_anchors(bundle, broken)
        changed = copy.deepcopy(bundle)
        changed["chunk_observations"][0]["anchors"]["E-T01-F"] = []
        with self.assertRaises(ProbeFailure):
            bind_anchors(changed, mapping)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from pydantic import ValidationError as PydanticValidationError

from knowledge_index_service.application.dtos import MetadataPatch, UnsetValue
from knowledge_index_service.presentation.schemas import (
    DocumentSubmissionRequest,
    MetadataPatchRequest,
)


class MetadataPatchContractTests(unittest.TestCase):
    def test_nullable_version_date_distinguishes_omitted_from_explicit_null(self) -> None:
        omitted = MetadataPatch()
        explicit_null = MetadataPatch(effective_at=None)

        self.assertIsInstance(omitted.effective_at, UnsetValue)
        self.assertIsNone(explicit_null.effective_at)

        omitted_request = MetadataPatchRequest()
        clear_request = MetadataPatchRequest(effective_at=None)
        self.assertNotIn("effective_at", omitted_request.model_fields_set)
        self.assertIn("effective_at", clear_request.model_fields_set)

    def test_nullable_source_url_distinguishes_omitted_from_explicit_null(self) -> None:
        omitted = MetadataPatch()
        explicit_null = MetadataPatch(source_url=None)

        self.assertIsInstance(omitted.source_url, UnsetValue)
        self.assertIsNone(explicit_null.source_url)

        omitted_request = MetadataPatchRequest()
        clear_request = MetadataPatchRequest(source_url=None)
        self.assertNotIn("source_url", omitted_request.model_fields_set)
        self.assertIn("source_url", clear_request.model_fields_set)

    def test_non_nullable_version_identifiers_reject_explicit_null(self) -> None:
        with self.assertRaises(PydanticValidationError):
            MetadataPatchRequest(version_number=None)
        with self.assertRaises(PydanticValidationError):
            MetadataPatchRequest(source_version=None)

    def test_submission_and_patch_reject_duplicate_acl_entries(self) -> None:
        duplicate_acl = [
            {"principal_type": "group", "principal_id": "employees", "effect": "allow"},
            {"principal_type": "group", "principal_id": "employees", "effect": "allow"},
        ]
        with self.assertRaises(PydanticValidationError):
            MetadataPatchRequest.model_validate({"acl": duplicate_acl})
        with self.assertRaises(PydanticValidationError):
            DocumentSubmissionRequest.model_validate(
                {
                    "source_type": "local_folder",
                    "source_item_id": "policy.md",
                    "source_version": "mtime:1",
                    "title": "Policy",
                    "document_type": "policy",
                    "owner_department": "HR",
                    "document_owner": "hr@example.com",
                    "version_number": "1.0",
                    "content_hash": "a" * 64,
                    "mime_type": "text/markdown",
                    "file_size": 100,
                    "parser_name": "markdown",
                    "parser_version": "1",
                    "chunks": [{"content": "Policy content"}],
                    "acl": duplicate_acl,
                }
            )


if __name__ == "__main__":
    unittest.main()

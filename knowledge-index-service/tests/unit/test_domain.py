from __future__ import annotations

import unittest
from dataclasses import replace
from uuid import uuid4

from knowledge_index_service.domain.entities import AccessControlEntry, Document
from knowledge_index_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    PermissionEffect,
    PrincipalType,
)
from knowledge_index_service.domain.exceptions import ValidationError


class DocumentAclTests(unittest.TestCase):
    def _document(self, acl: list[AccessControlEntry]) -> Document:
        return Document(
            id=uuid4(),
            source_type="folder",
            source_item_id="policy.md",
            title="Policy",
            document_type="policy",
            owner_department="IT",
            document_owner="owner@example.com",
            confidentiality=Confidentiality.INTERNAL,
            status=DocumentStatus.PUBLISHED,
            language="zh-TW",
            external_llm_allowed=False,
            acl=acl,
        )

    def test_document_without_allow_acl_is_public_inside_service(self) -> None:
        self.assertTrue(self._document([]).is_acl_public)

    def test_user_and_group_acl_are_projected_separately(self) -> None:
        document = self._document(
            [
                AccessControlEntry(PrincipalType.USER, "user-1"),
                AccessControlEntry(PrincipalType.GROUP, "group-1"),
            ]
        )

        self.assertFalse(document.is_acl_public)
        self.assertEqual(document.allowed_user_ids, ["user-1"])
        self.assertEqual(document.allowed_group_ids, ["group-1"])

    def test_deny_acl_is_projected_for_vector_filtering(self) -> None:
        document = self._document(
            [
                AccessControlEntry(
                    PrincipalType.USER,
                    "blocked-user",
                    PermissionEffect.DENY,
                )
            ]
        )

        self.assertEqual(document.denied_user_ids, ["blocked-user"])

    def test_restricted_document_requires_explicit_allow_acl(self) -> None:
        with self.assertRaises(ValidationError):
            replace(
                self._document([]),
                confidentiality=Confidentiality.RESTRICTED,
            )

    def test_document_rejects_duplicate_acl_entries(self) -> None:
        duplicate = AccessControlEntry(PrincipalType.GROUP, "employees")

        with self.assertRaisesRegex(ValidationError, "Duplicate ACL entry"):
            self._document([duplicate, duplicate])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ingestion_service.domain.entities import (
    AccessControlEntry,
    DiscoveredFile,
    DocumentMetadata,
    IngestionJob,
    SourceItem,
)
from ingestion_service.domain.enums import (
    Confidentiality,
    IngestionJobStatus,
    IngestionJobType,
    PendingOperation,
    PermissionEffect,
    PrincipalType,
    SourceItemStatus,
)
from ingestion_service.domain.exceptions import MetadataMissingError, ValidationError


def test_required_metadata_is_never_guessed() -> None:
    with pytest.raises(MetadataMissingError) as caught:
        DocumentMetadata(
            title="",
            document_type="policy",
            owner_department="",
            document_owner="owner@example.com",
        )

    assert caught.value.missing_fields == ("owner_department", "title")


def test_restricted_metadata_requires_allow_acl() -> None:
    with pytest.raises(ValidationError, match="require at least one allow ACL"):
        DocumentMetadata(
            title="Security Policy",
            document_type="policy",
            owner_department="Security",
            document_owner="security@example.com",
            confidentiality=Confidentiality.RESTRICTED,
            acl=(
                AccessControlEntry(
                    principal_type=PrincipalType.GROUP,
                    principal_id="contractors",
                    effect=PermissionEffect.DENY,
                ),
            ),
        )


def test_metadata_fingerprint_is_stable_when_acl_order_changes() -> None:
    entries = (
        AccessControlEntry(PrincipalType.GROUP, "employees"),
        AccessControlEntry(PrincipalType.USER, "user-1"),
    )
    kwargs = {
        "title": "Leave Policy",
        "document_type": "policy",
        "owner_department": "HR",
        "document_owner": "hr@example.com",
    }
    first = DocumentMetadata(**kwargs, acl=entries)
    second = DocumentMetadata(**kwargs, acl=tuple(reversed(entries)))

    assert first.fingerprint() == second.fingerprint()


def test_metadata_rejects_exact_duplicate_acl_entries() -> None:
    with pytest.raises(ValidationError, match="duplicate entries"):
        DocumentMetadata(
            title="Security Policy",
            document_type="policy",
            owner_department="Security",
            document_owner="security@example.com",
            acl=(
                AccessControlEntry(PrincipalType.GROUP, "employees"),
                AccessControlEntry(PrincipalType.GROUP, "employees"),
            ),
        )


def test_metadata_allows_distinct_effects_for_the_same_acl_principal() -> None:
    metadata = DocumentMetadata(
        title="Security Policy",
        document_type="policy",
        owner_department="Security",
        document_owner="security@example.com",
        acl=(
            AccessControlEntry(PrincipalType.GROUP, "employees"),
            AccessControlEntry(
                PrincipalType.GROUP,
                "employees",
                PermissionEffect.DENY,
            ),
        ),
    )

    assert len(metadata.acl) == 2


def test_job_lease_and_exponential_retry() -> None:
    now = datetime(2026, 8, 2, tzinfo=UTC)
    job = IngestionJob(
        id=uuid4(),
        source_id=uuid4(),
        item_id=uuid4(),
        job_type=IngestionJobType.PROCESS_ITEM,
        idempotency_key="process:one",
        available_at=now,
        created_at=now,
        updated_at=now,
    )

    job.claim("worker-1", now, timedelta(minutes=1))
    job.record_failure(
        "worker-1",
        now,
        code="temporary",
        message="try again",
        retryable=True,
        base_delay=timedelta(seconds=30),
    )

    assert job.status == IngestionJobStatus.PENDING
    assert job.attempt_count == 1
    assert job.available_at == now + timedelta(seconds=30)
    assert job.leased_by is None


def test_only_failed_job_can_be_manually_retried() -> None:
    job = IngestionJob(
        id=uuid4(),
        source_id=uuid4(),
        item_id=uuid4(),
        job_type=IngestionJobType.PROCESS_ITEM,
        idempotency_key="process:two",
    )

    with pytest.raises(ValidationError, match="Only failed jobs"):
        job.retry_manually(datetime.now(UTC))


def test_reappearing_file_waits_for_delete_then_restores_same_remote_identity() -> None:
    now = datetime(2026, 8, 2, tzinfo=UTC)
    document_id = uuid4()
    item = SourceItem(
        id=uuid4(),
        source_id=uuid4(),
        source_item_id="policy.md",
        source_path="policy.md",
        absolute_path="/data/policy.md",
        source_version="old",
        metadata_source_hash="1" * 64,
        mime_type="text/markdown",
        file_size=100,
        modified_at=now,
        status=SourceItemStatus.DELETING,
        missing_scan_count=2,
        pending_operation=PendingOperation.DELETE_DOCUMENT,
        knowledge_index_document_id=document_id,
        knowledge_index_job_id=uuid4(),
    )
    discovered = DiscoveredFile(
        source_item_id="policy.md",
        relative_path="policy.md",
        absolute_path=Path("/data/policy.md"),
        file_size=100,
        modified_at=now,
    )

    item.observe(discovered, "1" * 64, uuid4(), now)
    item.complete_pending_operation(now)

    assert item.status == SourceItemStatus.PENDING
    assert item.restore_requested is False
    assert item.knowledge_index_document_id == document_id
    assert item.deleted_at is None


def test_file_that_disappears_again_while_delete_is_in_flight_stays_missing() -> None:
    now = datetime(2026, 8, 2, tzinfo=UTC)
    item = SourceItem(
        id=uuid4(),
        source_id=uuid4(),
        source_item_id="policy.md",
        source_path="policy.md",
        absolute_path="/data/policy.md",
        source_version="old",
        metadata_source_hash="1" * 64,
        mime_type="text/markdown",
        file_size=100,
        modified_at=now,
        status=SourceItemStatus.DELETING,
        pending_operation=PendingOperation.DELETE_DOCUMENT,
        restore_requested=True,
        knowledge_index_document_id=uuid4(),
        knowledge_index_job_id=uuid4(),
    )

    item.mark_missing(now)
    item.complete_pending_operation(now)

    assert item.status == SourceItemStatus.MISSING
    assert item.missing_scan_count == 1
    assert item.restore_requested is False
    assert item.deleted_at is None

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from ingestion_service.domain.entities import IngestionJob, SourceItem
from ingestion_service.domain.enums import (
    IngestionJobType,
    PendingOperation,
    SourceItemStatus,
)
from ingestion_service.infrastructure.persistence import repositories


def test_source_item_mapping_preserves_file_and_restore_state() -> None:
    now = datetime(2026, 8, 2, 2, 0, tzinfo=UTC)
    source_item_id = f"nested/{'a' * 900}.md"
    item = SourceItem(
        id=uuid4(),
        source_id=uuid4(),
        source_item_id=source_item_id,
        source_path=source_item_id,
        absolute_path=f"/data/documents/{source_item_id}",
        source_version="mtime:1;size:100",
        metadata_source_hash="a" * 64,
        mime_type="text/markdown",
        file_size=100,
        modified_at=now,
        status=SourceItemStatus.DELETING,
        pending_operation=PendingOperation.DELETE_DOCUMENT,
        restore_requested=True,
        created_at=now,
        updated_at=now,
    )

    row = repositories._item_model(item)
    restored = repositories._item_entity(row)

    assert restored == item
    assert restored.modified_at == now
    assert restored.restore_requested is True
    assert len(row.source_item_hash) == 64


def test_ingestion_job_mapping_uses_domain_item_id() -> None:
    now = datetime(2026, 8, 2, 2, 0, tzinfo=UTC)
    job = IngestionJob(
        id=uuid4(),
        source_id=uuid4(),
        item_id=uuid4(),
        job_type=IngestionJobType.PROCESS_ITEM,
        idempotency_key="process:item:fingerprint",
        max_attempts=9,
        available_at=now,
        created_at=now,
        updated_at=now,
    )

    row = repositories._job_model(job)
    restored = repositories._job_entity(row)

    assert row.item_id == job.item_id
    assert restored == job

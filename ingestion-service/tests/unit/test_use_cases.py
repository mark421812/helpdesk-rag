from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ingestion_service.application.dtos import KnowledgeIndexJobResult
from ingestion_service.application.use_cases import (
    ClaimNextIngestionJob,
    IngestFile,
    ProcessIngestionJob,
    RetryIngestionJob,
    ScanSource,
    _process_job_key,
)
from ingestion_service.domain.entities import (
    Chunk,
    DiscoveredFile,
    DocumentMetadata,
    IngestionJob,
    ParsedBlock,
    ParsedDocument,
    Source,
    SourceItem,
)
from ingestion_service.domain.enums import (
    IngestionJobStatus,
    IngestionJobType,
    KnowledgeIndexingStatus,
    KnowledgeIndexJobStatus,
    PendingOperation,
    SourceItemStatus,
    SyncRunStatus,
)
from ingestion_service.domain.exceptions import (
    ExternalServiceError,
    MetadataMissingError,
    ValidationError,
)
from tests.unit.fakes import (
    FakeChunker,
    FakeKnowledgeIndexClient,
    FakeMetadataLoader,
    FakeParser,
    FakeParserRegistry,
    FakeScanner,
    FakeUnitOfWork,
)

NOW = datetime(2026, 8, 2, 2, 0, tzinfo=UTC)


def _clock() -> datetime:
    return NOW


def _source() -> Source:
    return Source(id=uuid4(), name="company", root_path="/data/documents")


def _file(
    name: str = "hr/leave.md",
    *,
    modified_at: datetime = NOW,
    file_size: int = 128,
    absolute_path: Path | None = None,
) -> DiscoveredFile:
    return DiscoveredFile(
        source_item_id=name,
        relative_path=name,
        absolute_path=absolute_path or Path("/data/documents") / name,
        file_size=file_size,
        modified_at=modified_at,
    )


def _metadata(title: str = "Leave Policy") -> DocumentMetadata:
    return DocumentMetadata(
        title=title,
        document_type="policy",
        owner_department="HR",
        document_owner="hr@example.com",
    )


def _item_source_version_reader(uow: FakeUnitOfWork):
    def read(path: Path) -> str:
        return next(
            item.source_version
            for item in uow.items.values.values()
            if item.absolute_path == str(path)
        )

    return read


async def _scan(
    uow: FakeUnitOfWork,
    source: Source,
    scanner: FakeScanner,
    metadata_loader: FakeMetadataLoader,
    *,
    max_missing_ratio: float = 0.25,
    max_job_attempts: int = 5,
    allow_mass_deletion: bool = False,
):
    use_case = ScanSource(
        uow,
        scanner,
        metadata_loader,
        clock=_clock,
        max_missing_ratio=max_missing_ratio,
        max_job_attempts=max_job_attempts,
    )
    return await use_case.execute(source.id, allow_mass_deletion=allow_mass_deletion)


async def _claim_and_process(
    uow: FakeUnitOfWork,
    metadata_loader: FakeMetadataLoader,
    knowledge_index: FakeKnowledgeIndexClient,
):
    claim = ClaimNextIngestionJob(uow, clock=_clock)
    view = await claim.execute("worker-1")
    assert view is not None
    processor = ProcessIngestionJob(
        uow,
        FakeParserRegistry(FakeParser()),
        FakeChunker(),
        metadata_loader,
        knowledge_index,
        source_version_reader=_item_source_version_reader(uow),
        clock=_clock,
    )
    return await processor.execute(view.id, "worker-1")


@pytest.mark.asyncio
async def test_scan_is_deterministic_and_does_not_duplicate_jobs() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())

    first = await _scan(uow, source, scanner, metadata_loader, max_job_attempts=7)
    second = await _scan(uow, source, scanner, metadata_loader, max_job_attempts=7)

    assert first.queued_count == 1
    assert second.queued_count == 0
    assert len(uow.jobs.values) == 1
    assert next(iter(uow.jobs.values.values())).max_attempts == 7


def test_process_job_key_uses_only_discovery_and_lifecycle_state() -> None:
    discovered = _file()
    item = SourceItem(
        id=uuid4(),
        source_id=uuid4(),
        source_item_id=discovered.source_item_id,
        source_path=discovered.relative_path,
        absolute_path=str(discovered.absolute_path),
        source_version=discovered.source_version,
        metadata_source_hash="1" * 64,
        mime_type="text/markdown",
        file_size=discovered.file_size,
        modified_at=discovered.modified_at,
    )
    original = _process_job_key(item)

    item.status = SourceItemStatus.WAITING_FOR_INDEX
    item.missing_scan_count = 1
    item.pending_operation = PendingOperation.PATCH_METADATA
    item.last_successful_content_hash = "a" * 64
    item.last_successful_metadata_hash = "b" * 64

    assert _process_job_key(item) == original

    item.source_version = "mtime:new;size:128"
    assert _process_job_key(item) != original
    item.source_version = discovered.source_version
    item.lifecycle_generation += 1
    assert _process_job_key(item) != original


@pytest.mark.asyncio
async def test_process_submits_namespaced_bounded_identity_and_completes() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    long_name = f"nested/{'a' * 900}.md"
    scanner = FakeScanner((_file(long_name),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    await _scan(uow, source, scanner, metadata_loader)

    result = await _claim_and_process(uow, metadata_loader, knowledge_index)

    assert result.status == IngestionJobStatus.COMPLETED
    assert result.item_status == SourceItemStatus.COMPLETED
    assert len(knowledge_index.submit_calls) == 1
    remote_identity = knowledge_index.submit_calls[0].source_item_id
    assert remote_identity.startswith(f"local:{source.id}:")
    assert len(remote_identity) < 512
    assert long_name not in remote_identity


@pytest.mark.asyncio
async def test_metadata_only_change_uses_patch_not_reembedding() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)

    metadata_loader.metadata = _metadata("Updated Leave Policy")
    metadata_loader.source_hash = "2" * 64
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)

    assert len(knowledge_index.submit_calls) == 1
    assert len(knowledge_index.patch_calls) == 1
    assert knowledge_index.patch_calls[0][1].title == "Updated Leave Policy"


@pytest.mark.asyncio
async def test_source_version_only_change_is_patched_without_reembedding() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)

    changed_file = _file(modified_at=NOW + timedelta(seconds=1))
    scanner.files = (changed_file,)
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)

    assert len(knowledge_index.submit_calls) == 1
    assert len(knowledge_index.patch_calls) == 1
    assert knowledge_index.patch_calls[0][1].source_version == changed_file.source_version


@pytest.mark.asyncio
async def test_missing_metadata_quarantines_without_guessing() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    metadata_loader.error = MetadataMissingError(("title",))
    knowledge_index = FakeKnowledgeIndexClient()
    await _scan(uow, source, scanner, metadata_loader)

    result = await _claim_and_process(uow, metadata_loader, knowledge_index)

    assert result.status == IngestionJobStatus.FAILED
    assert result.item_status == SourceItemStatus.METADATA_MISSING
    assert result.error_code == "metadata_missing"
    assert knowledge_index.submit_calls == []


@pytest.mark.asyncio
async def test_pending_remote_job_is_polled_to_completion() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    knowledge_index.submit_status = KnowledgeIndexingStatus.PENDING
    await _scan(uow, source, scanner, metadata_loader)

    first = await _claim_and_process(uow, metadata_loader, knowledge_index)
    second = await _claim_and_process(uow, metadata_loader, knowledge_index)

    assert first.item_status == SourceItemStatus.WAITING_FOR_INDEX
    assert second.status == IngestionJobStatus.COMPLETED
    assert second.item_status == SourceItemStatus.COMPLETED


@pytest.mark.asyncio
async def test_terminal_failed_submit_poll_can_be_manually_retried_as_new_submit() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    knowledge_index.submit_status = KnowledgeIndexingStatus.PENDING
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)
    knowledge_index.poll_results.append(
        KnowledgeIndexJobResult(
            job_id=knowledge_index.remote_job_id,
            document_id=knowledge_index.document_id,
            version_id=knowledge_index.version_id,
            status=KnowledgeIndexJobStatus.FAILED,
            error_message="embedding failed",
        )
    )

    failed = await _claim_and_process(uow, metadata_loader, knowledge_index)
    item = uow.items.values[failed.item_id]

    assert failed.status == IngestionJobStatus.FAILED
    assert item.pending_operation == PendingOperation.SUBMIT_CONTENT
    assert item.knowledge_index_job_id is None

    knowledge_index.submit_status = KnowledgeIndexingStatus.INDEXED
    await RetryIngestionJob(uow, clock=_clock).execute(failed.job_id)
    retried = await _claim_and_process(uow, metadata_loader, knowledge_index)

    assert retried.status == IngestionJobStatus.COMPLETED
    assert retried.item_status == SourceItemStatus.COMPLETED
    assert len(knowledge_index.submit_calls) == 2


@pytest.mark.asyncio
async def test_terminal_failed_patch_poll_can_be_manually_retried_as_new_patch() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)

    metadata_loader.metadata = _metadata("Updated Leave Policy")
    metadata_loader.source_hash = "2" * 64
    knowledge_index.patch_status = KnowledgeIndexJobStatus.PENDING
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)
    knowledge_index.poll_results.append(
        KnowledgeIndexJobResult(
            job_id=knowledge_index.remote_job_id,
            document_id=knowledge_index.document_id,
            version_id=knowledge_index.version_id,
            status=KnowledgeIndexJobStatus.FAILED,
            error_message="metadata patch failed",
        )
    )

    failed = await _claim_and_process(uow, metadata_loader, knowledge_index)
    item = uow.items.values[failed.item_id]

    assert item.pending_operation == PendingOperation.PATCH_METADATA
    assert item.knowledge_index_job_id is None

    knowledge_index.patch_status = KnowledgeIndexJobStatus.COMPLETED
    await RetryIngestionJob(uow, clock=_clock).execute(failed.job_id)
    retried = await _claim_and_process(uow, metadata_loader, knowledge_index)

    assert retried.status == IngestionJobStatus.COMPLETED
    assert retried.item_status == SourceItemStatus.COMPLETED
    assert len(knowledge_index.patch_calls) == 2


@pytest.mark.asyncio
async def test_terminal_failed_delete_poll_can_be_manually_retried_as_new_delete() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)

    scanner.files = ()
    await _scan(uow, source, scanner, metadata_loader, allow_mass_deletion=True)
    await _scan(uow, source, scanner, metadata_loader, allow_mass_deletion=True)
    knowledge_index.delete_status = KnowledgeIndexJobStatus.PENDING
    await _claim_and_process(uow, metadata_loader, knowledge_index)
    knowledge_index.poll_results.append(
        KnowledgeIndexJobResult(
            job_id=knowledge_index.remote_job_id,
            document_id=knowledge_index.document_id,
            version_id=knowledge_index.version_id,
            status=KnowledgeIndexJobStatus.FAILED,
            error_message="vector delete failed",
        )
    )

    failed = await _claim_and_process(uow, metadata_loader, knowledge_index)
    item = uow.items.values[failed.item_id]

    assert item.pending_operation == PendingOperation.DELETE_DOCUMENT
    assert item.knowledge_index_job_id is None

    knowledge_index.delete_status = KnowledgeIndexJobStatus.COMPLETED
    await RetryIngestionJob(uow, clock=_clock).execute(failed.job_id)
    retried = await _claim_and_process(uow, metadata_loader, knowledge_index)

    assert retried.status == IngestionJobStatus.COMPLETED
    assert retried.item_status == SourceItemStatus.DELETED
    assert len(knowledge_index.delete_calls) == 2


@pytest.mark.asyncio
async def test_transient_knowledge_index_failure_is_retried_with_backoff() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    knowledge_index.submit_error = ExternalServiceError("temporarily unavailable")
    await _scan(uow, source, scanner, metadata_loader)

    result = await _claim_and_process(uow, metadata_loader, knowledge_index)

    assert result.status == IngestionJobStatus.PENDING
    assert result.item_status == SourceItemStatus.FAILED
    assert uow.jobs.values[result.job_id].attempt_count == 1


@pytest.mark.asyncio
async def test_two_healthy_missing_scans_queue_soft_delete() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    files = tuple(_file(f"doc-{index}.md") for index in range(5))
    scanner = FakeScanner(files)
    metadata_loader = FakeMetadataLoader(_metadata())
    await _scan(uow, source, scanner, metadata_loader)
    removed = next(item for item in uow.items.values.values() if item.source_item_id == "doc-0.md")
    removed.knowledge_index_document_id = uuid4()

    scanner.files = files[1:]
    first_missing = await _scan(uow, source, scanner, metadata_loader)
    second_missing = await _scan(uow, source, scanner, metadata_loader)

    assert first_missing.missing_count == 1
    assert first_missing.delete_queued_count == 0
    assert second_missing.delete_queued_count == 1
    assert removed.missing_scan_count == 2
    assert any(job.job_type == IngestionJobType.DELETE_ITEM for job in uow.jobs.values.values())


@pytest.mark.asyncio
async def test_delete_waits_for_pending_submit_poll_before_replacing_remote_state() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    knowledge_index.submit_status = KnowledgeIndexingStatus.PENDING
    await _scan(uow, source, scanner, metadata_loader)
    process_job = await ClaimNextIngestionJob(uow, clock=_clock).execute("worker-process")
    assert process_job is not None
    processor = ProcessIngestionJob(
        uow,
        FakeParserRegistry(FakeParser()),
        FakeChunker(),
        metadata_loader,
        knowledge_index,
        poll_interval=timedelta(0),
        source_version_reader=_item_source_version_reader(uow),
        clock=_clock,
    )
    await processor.execute(process_job.id, "worker-process")

    scanner.files = ()
    await _scan(uow, source, scanner, metadata_loader, allow_mass_deletion=True)
    await _scan(uow, source, scanner, metadata_loader, allow_mass_deletion=True)
    delete_job = next(
        job for job in uow.jobs.values.values() if job.job_type == IngestionJobType.DELETE_ITEM
    )
    poll_job = next(
        job
        for job in uow.jobs.values.values()
        if job.job_type == IngestionJobType.POLL_KNOWLEDGE_INDEX
    )
    delete_job.claim("worker-delete", NOW, timedelta(minutes=10))

    deferred = await processor.execute(delete_job.id, "worker-delete")
    item = next(iter(uow.items.values.values()))

    assert deferred.deferred is True
    assert item.pending_operation == PendingOperation.SUBMIT_CONTENT
    assert item.knowledge_index_job_id == knowledge_index.remote_job_id
    assert knowledge_index.delete_calls == []

    poll_job.claim("worker-poll", NOW, timedelta(minutes=10))
    await processor.execute(poll_job.id, "worker-poll")
    delete_job.claim("worker-delete", NOW, timedelta(minutes=10))
    completed = await processor.execute(delete_job.id, "worker-delete")

    assert completed.item_status == SourceItemStatus.DELETED
    assert knowledge_index.delete_calls == [knowledge_index.document_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending_operation",
    [PendingOperation.SUBMIT_CONTENT, PendingOperation.PATCH_METADATA],
)
async def test_delete_defers_any_active_non_delete_remote_operation(
    pending_operation: PendingOperation,
) -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    discovered = _file()
    item = SourceItem(
        id=uuid4(),
        source_id=source.id,
        source_item_id=discovered.source_item_id,
        source_path=discovered.relative_path,
        absolute_path=str(discovered.absolute_path),
        source_version=discovered.source_version,
        metadata_source_hash="1" * 64,
        mime_type="text/markdown",
        file_size=discovered.file_size,
        modified_at=discovered.modified_at,
        status=SourceItemStatus.MISSING,
        missing_scan_count=2,
        pending_operation=pending_operation,
        knowledge_index_document_id=uuid4(),
        knowledge_index_job_id=uuid4(),
    )
    await uow.items.add(item)
    job = IngestionJob(
        id=uuid4(),
        source_id=source.id,
        item_id=item.id,
        job_type=IngestionJobType.DELETE_ITEM,
        idempotency_key=f"delete:{item.id}",
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    job.claim("worker-delete", NOW, timedelta(minutes=10))
    await uow.jobs.add(job)
    knowledge_index = FakeKnowledgeIndexClient()
    processor = ProcessIngestionJob(
        uow,
        FakeParserRegistry(FakeParser()),
        FakeChunker(),
        FakeMetadataLoader(_metadata()),
        knowledge_index,
        source_version_reader=_item_source_version_reader(uow),
        clock=_clock,
    )

    result = await processor.execute(job.id, "worker-delete")

    assert result.deferred is True
    assert item.pending_operation == pending_operation
    assert knowledge_index.delete_calls == []


@pytest.mark.asyncio
async def test_mass_deletion_guard_fails_scan_without_incrementing_counters() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    files = tuple(_file(f"doc-{index}.md") for index in range(5))
    scanner = FakeScanner(files)
    metadata_loader = FakeMetadataLoader(_metadata())
    await _scan(uow, source, scanner, metadata_loader)
    scanner.files = files[:1]

    with pytest.raises(ValidationError, match="Deletion guard"):
        await _scan(uow, source, scanner, metadata_loader)

    assert all(item.missing_scan_count == 0 for item in uow.items.values.values())
    assert list(uow.sync_runs.values.values())[-1].status == SyncRunStatus.FAILED


@pytest.mark.asyncio
async def test_explicit_mass_deletion_still_requires_two_complete_scans() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    await _scan(uow, source, scanner, metadata_loader)
    item = next(iter(uow.items.values.values()))
    item.knowledge_index_document_id = uuid4()
    scanner.files = ()

    first = await _scan(
        uow,
        source,
        scanner,
        metadata_loader,
        allow_mass_deletion=True,
    )
    second = await _scan(
        uow,
        source,
        scanner,
        metadata_loader,
        allow_mass_deletion=True,
    )

    assert first.delete_queued_count == 0
    assert item.missing_scan_count == 2
    assert second.delete_queued_count == 1


@pytest.mark.asyncio
async def test_restored_document_can_be_deleted_again_in_a_new_lifecycle() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file(),))
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)

    scanner.files = ()
    await _scan(
        uow,
        source,
        scanner,
        metadata_loader,
        allow_mass_deletion=True,
    )
    first_delete_scan = await _scan(
        uow,
        source,
        scanner,
        metadata_loader,
        allow_mass_deletion=True,
    )
    await _claim_and_process(uow, metadata_loader, knowledge_index)

    scanner.files = (_file(),)
    await _scan(uow, source, scanner, metadata_loader)
    await _claim_and_process(uow, metadata_loader, knowledge_index)

    scanner.files = ()
    await _scan(
        uow,
        source,
        scanner,
        metadata_loader,
        allow_mass_deletion=True,
    )
    second_delete_scan = await _scan(
        uow,
        source,
        scanner,
        metadata_loader,
        allow_mass_deletion=True,
    )

    assert first_delete_scan.delete_queued_count == 1
    assert second_delete_scan.delete_queued_count == 1
    assert (
        len(
            [
                job
                for job in uow.jobs.values.values()
                if job.job_type == IngestionJobType.DELETE_ITEM
            ]
        )
        == 2
    )


@pytest.mark.asyncio
async def test_manual_ingest_rejects_unsafe_path_and_does_not_mark_others_missing() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    scanner = FakeScanner((_file("one.md"), _file("two.md")))
    metadata_loader = FakeMetadataLoader(_metadata())
    use_case = IngestFile(uow, scanner, metadata_loader, clock=_clock)

    with pytest.raises(ValidationError, match="relative POSIX path"):
        await use_case.execute(source.id, "../one.md")

    result = await use_case.execute(source.id, "one.md")

    assert result.queued is True
    assert len(uow.items.values) == 1
    assert next(iter(uow.items.values.values())).source_item_id == "one.md"


@pytest.mark.asyncio
async def test_stale_delete_job_does_not_delete_reappeared_file() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    discovered = _file()
    item = SourceItem(
        id=uuid4(),
        source_id=source.id,
        source_item_id=discovered.source_item_id,
        source_path=discovered.relative_path,
        absolute_path=str(discovered.absolute_path),
        source_version=discovered.source_version,
        metadata_source_hash="1" * 64,
        mime_type="text/markdown",
        file_size=discovered.file_size,
        modified_at=discovered.modified_at,
        status=SourceItemStatus.PENDING,
        missing_scan_count=0,
        knowledge_index_document_id=uuid4(),
    )
    await uow.items.add(item)
    job = IngestionJob(
        id=uuid4(),
        source_id=source.id,
        item_id=item.id,
        job_type=IngestionJobType.DELETE_ITEM,
        idempotency_key=f"delete:{item.id}",
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    job.claim("worker-1", NOW, timedelta(minutes=10))
    await uow.jobs.add(job)
    knowledge_index = FakeKnowledgeIndexClient()
    processor = ProcessIngestionJob(
        uow,
        FakeParserRegistry(FakeParser()),
        FakeChunker(),
        FakeMetadataLoader(_metadata()),
        knowledge_index,
        source_version_reader=_item_source_version_reader(uow),
        clock=_clock,
    )

    result = await processor.execute(job.id, "worker-1")

    assert result.status == IngestionJobStatus.COMPLETED
    assert knowledge_index.delete_calls == []


@pytest.mark.asyncio
async def test_file_deleted_after_discovery_is_retryable_and_never_submitted(
    tmp_path: Path,
) -> None:
    document = tmp_path / "policy.md"
    document.write_text("original policy", encoding="utf-8")
    file_stat = document.stat()
    discovered = _file(
        "policy.md",
        modified_at=datetime.fromtimestamp(file_stat.st_mtime, tz=UTC),
        file_size=file_stat.st_size,
        absolute_path=document,
    )
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    await _scan(uow, source, FakeScanner((discovered,)), metadata_loader)
    claim = await ClaimNextIngestionJob(uow, clock=_clock).execute("worker-1")
    assert claim is not None
    document.unlink()
    processor = ProcessIngestionJob(
        uow,
        FakeParserRegistry(FakeParser()),
        FakeChunker(),
        metadata_loader,
        knowledge_index,
        clock=_clock,
    )

    result = await processor.execute(claim.id, "worker-1")

    assert result.status == IngestionJobStatus.PENDING
    assert result.item_status == SourceItemStatus.FAILED
    assert result.error_code == "source_changed"
    assert knowledge_index.submit_calls == []


@pytest.mark.asyncio
async def test_file_modified_during_parse_is_retryable_and_never_submitted(
    tmp_path: Path,
) -> None:
    document = tmp_path / "policy.md"
    document.write_text("original policy", encoding="utf-8")
    file_stat = document.stat()
    discovered = _file(
        "policy.md",
        modified_at=datetime.fromtimestamp(file_stat.st_mtime, tz=UTC),
        file_size=file_stat.st_size,
        absolute_path=document,
    )
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    metadata_loader = FakeMetadataLoader(_metadata())
    knowledge_index = FakeKnowledgeIndexClient()
    await _scan(uow, source, FakeScanner((discovered,)), metadata_loader)
    claim = await ClaimNextIngestionJob(uow, clock=_clock).execute("worker-1")
    assert claim is not None
    parser = FakeParser()

    def replace_document(path: Path) -> None:
        path.write_text(
            "replacement policy with a different size",
            encoding="utf-8",
        )

    parser.on_parse = replace_document
    processor = ProcessIngestionJob(
        uow,
        FakeParserRegistry(parser),
        FakeChunker(),
        metadata_loader,
        knowledge_index,
        clock=_clock,
    )

    result = await processor.execute(claim.id, "worker-1")

    assert result.status == IngestionJobStatus.PENDING
    assert result.item_status == SourceItemStatus.FAILED
    assert result.error_code == "source_changed"
    assert knowledge_index.submit_calls == []


@pytest.mark.asyncio
async def test_extracted_character_cap_quarantines_before_submission() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    metadata_loader = FakeMetadataLoader(_metadata())
    await _scan(uow, source, FakeScanner((_file(),)), metadata_loader)
    claim = await ClaimNextIngestionJob(uow, clock=_clock).execute("worker-1")
    assert claim is not None
    parser = FakeParser()
    parser.document = ParsedDocument(
        blocks=(ParsedBlock("x" * 11),),
        parser_name="fake",
        parser_version="1",
        mime_type="text/plain",
    )
    knowledge_index = FakeKnowledgeIndexClient()
    processor = ProcessIngestionJob(
        uow,
        FakeParserRegistry(parser),
        FakeChunker(),
        metadata_loader,
        knowledge_index,
        max_document_characters=10,
        source_version_reader=_item_source_version_reader(uow),
        clock=_clock,
    )

    result = await processor.execute(claim.id, "worker-1")

    assert result.item_status == SourceItemStatus.QUARANTINED
    assert "11 characters; limit is 10" in (result.error_message or "")
    assert knowledge_index.submit_calls == []


@pytest.mark.asyncio
async def test_chunk_count_cap_quarantines_before_submission() -> None:
    uow = FakeUnitOfWork()
    source = _source()
    await uow.sources.add(source)
    metadata_loader = FakeMetadataLoader(_metadata())
    await _scan(uow, source, FakeScanner((_file(),)), metadata_loader)
    claim = await ClaimNextIngestionJob(uow, clock=_clock).execute("worker-1")
    assert claim is not None
    chunker = FakeChunker()
    chunker.chunks = (
        Chunk("first", None, None, 1),
        Chunk("second", None, None, 1),
    )
    knowledge_index = FakeKnowledgeIndexClient()
    processor = ProcessIngestionJob(
        uow,
        FakeParserRegistry(FakeParser()),
        chunker,
        metadata_loader,
        knowledge_index,
        max_document_chunks=1,
        source_version_reader=_item_source_version_reader(uow),
        clock=_clock,
    )

    result = await processor.execute(claim.id, "worker-1")

    assert result.item_status == SourceItemStatus.QUARANTINED
    assert "2 chunks; limit is 1" in (result.error_message or "")
    assert knowledge_index.submit_calls == []


def test_process_safety_caps_must_be_positive() -> None:
    dependencies = (
        FakeUnitOfWork(),
        FakeParserRegistry(FakeParser()),
        FakeChunker(),
        FakeMetadataLoader(_metadata()),
        FakeKnowledgeIndexClient(),
    )
    with pytest.raises(ValidationError, match="max_document_characters"):
        ProcessIngestionJob(*dependencies, max_document_characters=0)
    with pytest.raises(ValidationError, match="max_document_chunks"):
        ProcessIngestionJob(*dependencies, max_document_chunks=0)

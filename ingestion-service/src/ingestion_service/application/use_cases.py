"""Ingestion 的 Application use cases 與 job state-machine orchestration。

本模組負責 scan/change detection、enqueue、claim/process/retry 與 view queries，但不直接
使用 FastAPI、SQLAlchemy、PyMuPDF 或 YAML 套件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat as stat_module
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ingestion_service.application.dtos import (
    IngestFileResult,
    IngestionJobView,
    KnowledgeIndexDocumentSubmission,
    KnowledgeIndexJobResult,
    KnowledgeIndexMetadataPatch,
    ProcessJobResult,
    ScanSourceResult,
    SourceItemView,
    SyncRunView,
)
from ingestion_service.application.ports import (
    DocumentChunker,
    KnowledgeIndexClient,
    MetadataLoader,
    ParserRegistry,
    SourceScanLock,
    SourceScanner,
    UnitOfWork,
    UnitOfWorkFactory,
)
from ingestion_service.domain.entities import (
    Chunk,
    DiscoveredFile,
    DocumentMetadata,
    IngestionJob,
    ParsedDocument,
    Source,
    SourceItem,
    SyncRun,
    utc_now,
)
from ingestion_service.domain.enums import (
    IngestionJobType,
    KnowledgeIndexingStatus,
    KnowledgeIndexJobStatus,
    PendingOperation,
    SourceItemStatus,
    SyncRunStatus,
    SyncTrigger,
)
from ingestion_service.domain.exceptions import (
    ExternalServiceError,
    FileTooLargeError,
    LeaseLostError,
    MetadataMissingError,
    NotFoundError,
    ParserRequiresOcrError,
    SourceChangedError,
    UnsupportedDocumentError,
    ValidationError,
)

Clock = Callable[[], datetime]
SourceVersionReader = Callable[[Path], str]
# 上面是 type alias：把較長的 callable 型別取一個業務上可讀的名稱，也方便測試注入 fake。


class _NoopSourceScanLock:
    """測試或單程序環境的空 lock；介面相同但不真的鎖定。"""

    @asynccontextmanager
    async def acquire(self, source_id: UUID) -> AsyncIterator[None]:
        del source_id
        yield


class ScanSource:
    """完整掃描來源，比對前次狀態，排入可重入的處理／刪除 jobs。

    此 use case 只負責「發現與排程」，不在 API request 內解析大型文件。真正的 parser、
    chunker 與 KIS 呼叫交由背景 worker 執行。
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        scanner: SourceScanner,
        metadata_loader: MetadataLoader,
        *,
        scan_lock: SourceScanLock | None = None,
        missing_scans_before_delete: int = 2,
        max_missing_ratio: float = 0.25,
        max_job_attempts: int = 5,
        clock: Clock = utc_now,
    ) -> None:
        if missing_scans_before_delete < 2:
            raise ValidationError("missing_scans_before_delete must be at least two")
        if not 0 < max_missing_ratio <= 1:
            raise ValidationError("max_missing_ratio must be greater than zero and at most one")
        if max_job_attempts < 1:
            raise ValidationError("max_job_attempts must be at least one")
        self._uow_factory = uow_factory
        self._scanner = scanner
        self._metadata_loader = metadata_loader
        self._scan_lock = scan_lock or _NoopSourceScanLock()
        self._missing_scans_before_delete = missing_scans_before_delete
        self._max_missing_ratio = max_missing_ratio
        self._max_job_attempts = max_job_attempts
        self._clock = clock

    async def execute(
        self,
        source_id: UUID,
        trigger: SyncTrigger = SyncTrigger.MANUAL,
        *,
        allow_mass_deletion: bool = False,
    ) -> ScanSourceResult:
        # ``async with`` 取得 source-level lock；離開區塊時即使例外也會自動釋放。
        async with self._scan_lock.acquire(source_id):
            return await self._execute_locked(
                source_id,
                trigger,
                allow_mass_deletion=allow_mass_deletion,
            )

    async def _execute_locked(
        self,
        source_id: UUID,
        trigger: SyncTrigger,
        *,
        allow_mass_deletion: bool,
    ) -> ScanSourceResult:
        now = self._clock()
        run = SyncRun(id=uuid4(), source_id=source_id, trigger=trigger, started_at=now)
        # 短 transaction：先保存 run，再離開 DB transaction 執行較慢的檔案 I/O。
        async with self._uow_factory() as uow:
            source = await uow.sources.get(source_id)
            if source is None:
                raise NotFoundError(f"Source {source_id} was not found")
            if not source.enabled:
                raise ValidationError(f"Source {source_id} is disabled")
            existing_items = tuple(await uow.items.list_for_source(source_id))
            await uow.sync_runs.add(run)
            await uow.commit()

        try:
            # scanner 是同步檔案 I/O；to_thread 避免阻塞 FastAPI/worker 的 event loop。
            scanned = await asyncio.to_thread(self._scanner.scan, source)
            discovered = tuple(sorted(scanned, key=lambda value: value.source_item_id))
            source_ids = [entry.source_item_id for entry in discovered]
            if len(source_ids) != len(set(source_ids)):
                raise ValidationError("Scanner returned duplicate source_item_id values")
            fingerprints = await asyncio.to_thread(
                self._metadata_fingerprints,
                source,
                discovered,
            )
            # set comprehension 建立集合，之後可用 difference 快速找出消失的檔案。
            existing_active_ids = {
                item.source_item_id
                for item in existing_items
                if item.status != SourceItemStatus.DELETED
            }
            discovered_ids = set(source_ids)
            absent_count = len(existing_active_ids.difference(discovered_ids))
            # 安全護欄：來源掛載失敗時可能看起來「全部被刪除」，不可直接大量 soft delete。
            if existing_active_ids and not allow_mass_deletion:
                missing_ratio = absent_count / len(existing_active_ids)
                if not discovered or missing_ratio > self._max_missing_ratio:
                    raise ValidationError(
                        "Deletion guard rejected scan: "
                        f"{absent_count}/{len(existing_active_ids)} existing items are missing"
                    )
        except Exception as exc:
            await self._fail_run(run.id, str(exc))
            raise

        discovered_count = len(discovered)
        queued_count = 0
        missing_count = 0
        delete_queued_count = 0
        completed_at = self._clock()

        async with self._uow_factory() as uow:
            persisted_run = await uow.sync_runs.get(run.id)
            persisted_source = await uow.sources.get(source_id)
            if persisted_run is None or persisted_source is None:
                raise NotFoundError("Source or sync run disappeared while scanning")
            items = list(await uow.items.list_for_source(source_id))
            # dict comprehension 建立 O(1) lookup，避免每個檔案都線性掃描 items。
            by_source_item_id = {item.source_item_id: item for item in items}
            seen: set[str] = set()

            for entry in discovered:
                seen.add(entry.source_item_id)
                metadata_source_hash = fingerprints[entry.source_item_id]
                item = by_source_item_id.get(entry.source_item_id)
                if item is None:
                    # uuid5 對同一組 namespace+文字永遠產生相同 UUID，支援 idempotency。
                    item = SourceItem(
                        id=uuid5(
                            NAMESPACE_URL,
                            f"ingestion:{source_id}:{entry.source_item_id}",
                        ),
                        source_id=source_id,
                        source_item_id=entry.source_item_id,
                        source_path=entry.relative_path,
                        absolute_path=str(entry.absolute_path),
                        source_version=entry.source_version,
                        metadata_source_hash=metadata_source_hash,
                        mime_type=None,
                        file_size=entry.file_size,
                        modified_at=entry.modified_at,
                        status=SourceItemStatus.PENDING,
                        last_seen_run_id=run.id,
                        last_seen_at=completed_at,
                    )
                    await uow.items.add(item)
                    items.append(item)
                    by_source_item_id[item.source_item_id] = item
                    changed = True
                else:
                    changed = item.observe(entry, metadata_source_hash, run.id, completed_at)

                # 單一壞檔只隔離該 item，``continue`` 讓掃描繼續處理下一份文件。
                if entry.file_size > persisted_source.max_file_size_bytes:
                    error = FileTooLargeError(
                        f"File size {entry.file_size} exceeds configured maximum "
                        f"{persisted_source.max_file_size_bytes}"
                    )
                    item.quarantine(
                        SourceItemStatus.QUARANTINED,
                        "file_too_large",
                        str(error),
                        completed_at,
                    )
                    await uow.items.update(item)
                    continue

                needs_initial_index = item.last_successful_content_hash is None
                if changed or needs_initial_index:
                    key = _process_job_key(item)
                    if await _enqueue_job(
                        uow,
                        source_id=source_id,
                        item=item,
                        run_id=run.id,
                        job_type=IngestionJobType.PROCESS_ITEM,
                        idempotency_key=key,
                        max_attempts=self._max_job_attempts,
                        now=completed_at,
                    ):
                        queued_count += 1
                await uow.items.update(item)

            # 第二輪處理「DB 有、這次掃描沒有」的項目；連續達門檻才排刪除 job。
            for item in items:
                if item.source_item_id in seen or item.status == SourceItemStatus.DELETED:
                    continue
                missing_count += 1
                count = item.mark_missing(completed_at)
                if count >= self._missing_scans_before_delete:
                    if item.knowledge_index_document_id is None:
                        item.complete_without_remote_job(
                            operation=PendingOperation.DELETE_DOCUMENT,
                            content_hash=None,
                            metadata_hash=None,
                            document_id=None,
                            version_id=None,
                            at=completed_at,
                        )
                    elif await _enqueue_job(
                        uow,
                        source_id=source_id,
                        item=item,
                        run_id=run.id,
                        job_type=IngestionJobType.DELETE_ITEM,
                        idempotency_key=(
                            f"delete:{item.id}:{item.knowledge_index_document_id}:"
                            f"{item.lifecycle_generation}"
                        ),
                        max_attempts=self._max_job_attempts,
                        now=completed_at,
                    ):
                        delete_queued_count += 1
                await uow.items.update(item)

            persisted_run.complete(
                discovered_count=discovered_count,
                queued_count=queued_count,
                missing_count=missing_count,
                delete_queued_count=delete_queued_count,
                at=completed_at,
            )
            persisted_source.last_successful_scan_at = completed_at
            persisted_source.updated_at = completed_at
            await uow.sync_runs.update(persisted_run)
            await uow.sources.update(persisted_source)
            await uow.commit()

        return ScanSourceResult(
            run_id=run.id,
            source_id=source_id,
            status=SyncRunStatus.COMPLETED,
            discovered_count=discovered_count,
            queued_count=queued_count,
            missing_count=missing_count,
            delete_queued_count=delete_queued_count,
        )

    async def _fail_run(self, run_id: UUID, message: str) -> None:
        async with self._uow_factory() as uow:
            run = await uow.sync_runs.get(run_id)
            if run is not None:
                run.fail(message, self._clock())
                await uow.sync_runs.update(run)
                await uow.commit()

    def _metadata_fingerprints(
        self,
        source: Source,
        discovered: tuple[DiscoveredFile, ...],
    ) -> dict[str, str]:
        return {
            entry.source_item_id: self._metadata_loader.source_fingerprint(source, entry)
            for entry in discovered
        }


class IngestFile:
    """只發現並排入指定檔案，不把來源中的其他文件誤判為消失。

    適合 CLI 立即匯入單一文件；完整每日同步則使用 ``ScanSource``。
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        scanner: SourceScanner,
        metadata_loader: MetadataLoader,
        *,
        scan_lock: SourceScanLock | None = None,
        max_job_attempts: int = 5,
        clock: Clock = utc_now,
    ) -> None:
        if max_job_attempts < 1:
            raise ValidationError("max_job_attempts must be at least one")
        self._uow_factory = uow_factory
        self._scanner = scanner
        self._metadata_loader = metadata_loader
        self._scan_lock = scan_lock or _NoopSourceScanLock()
        self._max_job_attempts = max_job_attempts
        self._clock = clock

    async def execute(
        self,
        source_id: UUID,
        source_item_id: str,
        trigger: SyncTrigger = SyncTrigger.CLI,
    ) -> IngestFileResult:
        async with self._scan_lock.acquire(source_id):
            return await self._execute_locked(source_id, source_item_id, trigger)

    async def _execute_locked(
        self,
        source_id: UUID,
        source_item_id: str,
        trigger: SyncTrigger,
    ) -> IngestFileResult:
        source_item_id = _validated_relative_source_item_id(source_item_id)
        now = self._clock()
        run = SyncRun(id=uuid4(), source_id=source_id, trigger=trigger, started_at=now)
        async with self._uow_factory() as uow:
            source = await uow.sources.get(source_id)
            if source is None:
                raise NotFoundError(f"Source {source_id} was not found")
            if not source.enabled:
                raise ValidationError(f"Source {source_id} is disabled")
            await uow.sync_runs.add(run)
            await uow.commit()

        try:
            discovered = await asyncio.to_thread(
                self._scanner.discover,
                source,
                source_item_id,
            )
            if discovered is None:
                raise NotFoundError(f"File {source_item_id!r} was not found in source {source_id}")
            metadata_source_hash = await asyncio.to_thread(
                self._metadata_loader.source_fingerprint,
                source,
                discovered,
            )
        except Exception as exc:
            await self._fail_run(run.id, str(exc))
            raise

        now = self._clock()
        async with self._uow_factory() as uow:
            persisted_run = await uow.sync_runs.get(run.id)
            item = await uow.items.find_by_source_item_id(source_id, source_item_id)
            if persisted_run is None:
                raise NotFoundError(f"Sync run {run.id} was not found")
            if item is None:
                item = SourceItem(
                    id=uuid5(NAMESPACE_URL, f"ingestion:{source_id}:{source_item_id}"),
                    source_id=source_id,
                    source_item_id=source_item_id,
                    source_path=discovered.relative_path,
                    absolute_path=str(discovered.absolute_path),
                    source_version=discovered.source_version,
                    metadata_source_hash=metadata_source_hash,
                    mime_type=None,
                    file_size=discovered.file_size,
                    modified_at=discovered.modified_at,
                    status=SourceItemStatus.PENDING,
                    last_seen_run_id=run.id,
                    last_seen_at=now,
                )
                await uow.items.add(item)
            else:
                item.observe(discovered, metadata_source_hash, run.id, now)

            queued = False
            job_id: UUID | None = None
            if discovered.file_size > source.max_file_size_bytes:
                item.quarantine(
                    SourceItemStatus.QUARANTINED,
                    "file_too_large",
                    f"File size {discovered.file_size} exceeds configured maximum "
                    f"{source.max_file_size_bytes}",
                    now,
                )
            else:
                key = _process_job_key(item)
                queued = await _enqueue_job(
                    uow,
                    source_id=source_id,
                    item=item,
                    run_id=run.id,
                    job_type=IngestionJobType.PROCESS_ITEM,
                    idempotency_key=key,
                    max_attempts=self._max_job_attempts,
                    now=now,
                )
                job = await uow.jobs.get_by_idempotency_key(key)
                job_id = job.id if job is not None else None
            persisted_run.complete(
                discovered_count=1,
                queued_count=int(queued),
                missing_count=0,
                delete_queued_count=0,
                at=now,
            )
            await uow.items.update(item)
            await uow.sync_runs.update(persisted_run)
            await uow.commit()
            return IngestFileResult(
                run_id=run.id,
                item=_item_view(item),
                job_id=job_id,
                queued=queued,
            )

    async def _fail_run(self, run_id: UUID, message: str) -> None:
        async with self._uow_factory() as uow:
            run = await uow.sync_runs.get(run_id)
            if run is not None:
                run.fail(message, self._clock())
                await uow.sync_runs.update(run)
                await uow.commit()


class ClaimNextIngestionJob:
    """原子取得下一個可執行 job，並為目前 worker 建立限時 lease。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        lease_for: timedelta = timedelta(minutes=10),
        clock: Clock = utc_now,
    ) -> None:
        if lease_for <= timedelta(0):
            raise ValidationError("lease_for must be positive")
        self._uow_factory = uow_factory
        self._lease_for = lease_for
        self._clock = clock

    async def execute(self, worker_id: str) -> IngestionJobView | None:
        if not worker_id.strip():
            raise ValidationError("worker_id must not be blank")
        async with self._uow_factory() as uow:
            job = await uow.jobs.claim_next(
                worker_id=worker_id,
                now=self._clock(),
                lease_for=self._lease_for,
            )
            if job is None:
                return None
            await uow.commit()
            return _job_view(job)


class ProcessIngestionJob:
    """處理一個已取得 lease 的 job，慢速 I/O 期間不保持 DB transaction。

    這個 class 是 ingestion 核心 orchestrator：依 job type 分派、解析、載入 metadata、
    chunk、呼叫 KIS，並把不同例外分類為永久失敗或可重試失敗。
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        parser_registry: ParserRegistry,
        chunker: DocumentChunker,
        metadata_loader: MetadataLoader,
        knowledge_index: KnowledgeIndexClient,
        *,
        max_job_attempts: int = 5,
        max_document_characters: int = 20_000_000,
        max_document_chunks: int = 10_000,
        retry_base_delay: timedelta = timedelta(seconds=30),
        poll_interval: timedelta = timedelta(seconds=5),
        source_version_reader: SourceVersionReader | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if max_job_attempts < 1:
            raise ValidationError("max_job_attempts must be at least one")
        if max_document_characters < 1:
            raise ValidationError("max_document_characters must be at least one")
        if max_document_chunks < 1:
            raise ValidationError("max_document_chunks must be at least one")
        self._uow_factory = uow_factory
        self._parser_registry = parser_registry
        self._chunker = chunker
        self._metadata_loader = metadata_loader
        self._knowledge_index = knowledge_index
        self._max_job_attempts = max_job_attempts
        self._max_document_characters = max_document_characters
        self._max_document_chunks = max_document_chunks
        self._retry_base_delay = retry_base_delay
        self._poll_interval = poll_interval
        self._source_version_reader = source_version_reader or _source_version_for_path
        self._clock = clock

    async def execute(self, job_id: UUID, worker_id: str) -> ProcessJobResult:
        # 例外型別本身代表錯誤政策，讓主流程不需靠字串猜測是否可重試。
        try:
            job_type = await self._job_type(job_id, worker_id)
            if job_type == IngestionJobType.PROCESS_ITEM:
                deferred = await self._process_item(job_id, worker_id)
            elif job_type == IngestionJobType.POLL_KNOWLEDGE_INDEX:
                deferred = await self._poll_knowledge_index(job_id, worker_id)
            elif job_type == IngestionJobType.DELETE_ITEM:
                deferred = await self._delete_item(job_id, worker_id)
            else:  # pragma: no cover - StrEnum/database corruption guard
                raise ValidationError(f"Unsupported ingestion job type: {job_type}")
        except MetadataMissingError as exc:
            return await self._record_permanent_failure(
                job_id,
                worker_id,
                item_status=SourceItemStatus.METADATA_MISSING,
                code="metadata_missing",
                message=str(exc),
            )
        except ParserRequiresOcrError as exc:
            return await self._record_permanent_failure(
                job_id,
                worker_id,
                item_status=SourceItemStatus.OCR_REQUIRED,
                code="ocr_required",
                message=str(exc),
            )
        except (NotFoundError, LeaseLostError):
            raise
        except SourceChangedError as exc:
            return await self._record_retryable_failure(
                job_id,
                worker_id,
                code="source_changed",
                message=str(exc),
                retryable=True,
            )
        except (UnsupportedDocumentError, ValidationError) as exc:
            return await self._record_permanent_failure(
                job_id,
                worker_id,
                item_status=SourceItemStatus.QUARANTINED,
                code="invalid_document",
                message=str(exc),
            )
        except ExternalServiceError as exc:
            return await self._record_retryable_failure(
                job_id,
                worker_id,
                code="knowledge_index_error",
                message=str(exc),
                retryable=exc.retryable,
            )
        except Exception as exc:
            return await self._record_retryable_failure(
                job_id,
                worker_id,
                code="processing_error",
                message=str(exc),
                retryable=True,
            )

        async with self._uow_factory() as uow:
            job = await uow.jobs.get(job_id)
            if job is None:
                raise NotFoundError(f"Job {job_id} was not found")
            item = await uow.items.get(job.item_id)
            if item is None:
                raise NotFoundError(f"Source item {job.item_id} was not found")
            return ProcessJobResult(
                job_id=job.id,
                item_id=item.id,
                job_type=job.job_type,
                status=job.status,
                item_status=item.status,
                deferred=deferred,
                error_code=job.error_code,
                error_message=job.error_message,
            )

    async def _job_type(self, job_id: UUID, worker_id: str) -> IngestionJobType:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get(job_id)
            if job is None:
                raise NotFoundError(f"Job {job_id} was not found")
            job.assert_lease(worker_id, self._clock())
            return job.job_type

    async def _process_item(self, job_id: UUID, worker_id: str) -> bool:
        """執行 process job；回傳 True 代表延後處理，False 代表本次已結束。"""

        now = self._clock()
        async with self._uow_factory() as uow:
            job, item, source = await _load_job_context(uow, job_id)
            job.assert_lease(worker_id, now)
            retrying_failed_remote_operation = (
                job.job_type == IngestionJobType.POLL_KNOWLEDGE_INDEX
                and item.knowledge_index_job_id is None
                and item.pending_operation
                in {
                    PendingOperation.SUBMIT_CONTENT,
                    PendingOperation.PATCH_METADATA,
                }
            )
            if item.pending_operation is not None and not retrying_failed_remote_operation:
                job.defer(worker_id, now, self._poll_interval)
                await uow.jobs.update(job)
                await uow.commit()
                return True
            if item.status in {
                SourceItemStatus.MISSING,
                SourceItemStatus.DELETING,
                SourceItemStatus.DELETED,
            }:
                job.complete(worker_id, now)
                await uow.jobs.update(job)
                await uow.commit()
                return False
            expected_source_version = item.source_version
            expected_discovery_fingerprint = item.discovery_fingerprint
            item.begin(SourceItemStatus.PARSING, now)
            await uow.items.update(item)
            await uow.commit()

        # transaction 已關閉；接下來的 filesystem/parser/tokenizer I/O 不會占住 DB 連線。
        discovered = _discovered_from_item(item)
        path = Path(item.absolute_path)
        await self._assert_source_version(path, expected_source_version, "before parsing")
        # Registry pattern 將副檔名選擇邏輯集中起來，use case 不需要 import 每種 parser。
        parser = self._parser_registry.get_parser(path, item.mime_type)
        parsed = await asyncio.to_thread(parser.parse, path)
        await self._assert_source_version(path, expected_source_version, "after parsing")
        extracted_characters = sum(len(block.content) for block in parsed.blocks)
        if extracted_characters > self._max_document_characters:
            raise ValidationError(
                f"Document extracted content has {extracted_characters} characters; limit is "
                f"{self._max_document_characters}"
            )
        metadata = await asyncio.to_thread(
            self._metadata_loader.load,
            source,
            discovered,
            parsed,
        )
        chunks = await asyncio.to_thread(self._chunker.chunk, parsed)
        if not chunks:
            raise ValidationError("Chunker produced no chunks")
        if len(chunks) > self._max_document_chunks:
            raise ValidationError(
                f"Document produced {len(chunks)} chunks; limit is {self._max_document_chunks}"
            )
        # list comprehension 收集所有違規 index，錯誤訊息能精確指出問題位置。
        oversized_sections = [
            index
            for index, chunk in enumerate(chunks)
            if chunk.section_path is not None and len(chunk.section_path) > 2000
        ]
        if oversized_sections:
            raise ValidationError(
                "Chunk section_path exceeds 2000 characters at index(es): "
                + ", ".join(str(index) for index in oversized_sections[:10])
            )
        total_characters = sum(len(chunk.content) for chunk in chunks)
        if total_characters > self._max_document_characters:
            raise ValidationError(
                f"Document extracted content has {total_characters} characters; limit is "
                f"{self._max_document_characters}"
            )
        await self._assert_source_version(path, expected_source_version, "before submission")
        content_hash = _content_hash(parsed.parser_name, parsed.parser_version, chunks)
        metadata_hash = _metadata_state_hash(metadata, expected_source_version)

        now = self._clock()
        async with self._uow_factory() as uow:
            job, current_item, _ = await _load_job_context(uow, job_id)
            job.assert_lease(worker_id, now)
            if current_item.discovery_fingerprint != expected_discovery_fingerprint:
                raise SourceChangedError(
                    "Source discovery changed while the document was being processed"
                )
            current_item.mime_type = parsed.mime_type
            current_item.begin(SourceItemStatus.SUBMITTING, now)
            await uow.items.update(current_item)
            await uow.commit()

        # 決策一：沒有遠端文件，或內容 hash 改變，必須送完整 chunks 重新索引。
        if (
            current_item.knowledge_index_document_id is None
            or current_item.last_successful_content_hash != content_hash
        ):
            submission = _submission(current_item, metadata, parsed, chunks, content_hash)
            submission_result = await self._knowledge_index.submit_document(submission)
            if submission_result.indexing_status == KnowledgeIndexingStatus.FAILED:
                raise ExternalServiceError(
                    "Knowledge Index rejected document indexing", retryable=False
                )
            return await self._finish_or_wait(
                job_id=job_id,
                worker_id=worker_id,
                operation=PendingOperation.SUBMIT_CONTENT,
                content_hash=content_hash,
                metadata_hash=metadata_hash,
                result=KnowledgeIndexJobResult(
                    job_id=submission_result.job_id or uuid4(),
                    document_id=submission_result.document_id,
                    version_id=submission_result.version_id,
                    status=(
                        KnowledgeIndexJobStatus.COMPLETED
                        if submission_result.indexing_status == KnowledgeIndexingStatus.INDEXED
                        else KnowledgeIndexJobStatus.PENDING
                    ),
                ),
                remote_job_exists=submission_result.job_id is not None,
            )

        # 決策二：內容相同而 metadata hash 改變，只 PATCH metadata，避免重算 embedding。
        if current_item.last_successful_metadata_hash != metadata_hash:
            document_id = current_item.knowledge_index_document_id
            if document_id is None:  # narrowed above, defensive for concurrent mutation
                raise ValidationError("Metadata patch requires a Knowledge Index document ID")
            patch_result = await self._knowledge_index.patch_metadata(
                document_id, _metadata_patch(current_item, metadata)
            )
            return await self._finish_or_wait(
                job_id=job_id,
                worker_id=worker_id,
                operation=PendingOperation.PATCH_METADATA,
                content_hash=content_hash,
                metadata_hash=metadata_hash,
                result=patch_result,
                remote_job_exists=True,
            )

        # 決策三：內容與 metadata 都相同，直接完成 idempotent job，不呼叫遠端服務。
        now = self._clock()
        async with self._uow_factory() as uow:
            job, item, _ = await _load_job_context(uow, job_id)
            job.assert_lease(worker_id, now)
            item.complete_without_remote_job(
                operation=PendingOperation.PATCH_METADATA,
                content_hash=content_hash,
                metadata_hash=metadata_hash,
                document_id=item.knowledge_index_document_id,
                version_id=item.knowledge_index_version_id,
                at=now,
            )
            job.complete(worker_id, now)
            await uow.items.update(item)
            await uow.jobs.update(job)
            await uow.commit()
        return False

    async def _delete_item(self, job_id: UUID, worker_id: str) -> bool:
        now = self._clock()
        async with self._uow_factory() as uow:
            job, item, _ = await _load_job_context(uow, job_id)
            job.assert_lease(worker_id, now)
            if item.pending_operation is not None and item.knowledge_index_job_id is not None:
                job.defer(worker_id, now, self._poll_interval)
                await uow.jobs.update(job)
                await uow.commit()
                return True
            if (
                item.missing_scan_count == 0
                and item.pending_operation != PendingOperation.DELETE_DOCUMENT
                and item.status
                not in {
                    SourceItemStatus.MISSING,
                    SourceItemStatus.DELETING,
                }
            ):
                job.complete(worker_id, now)
                await uow.jobs.update(job)
                await uow.commit()
                return False
            document_id = item.knowledge_index_document_id
            item.begin_delete(now)
            await uow.items.update(item)
            await uow.commit()

        if document_id is None:
            async with self._uow_factory() as uow:
                job, item, _ = await _load_job_context(uow, job_id)
                now = self._clock()
                job.assert_lease(worker_id, now)
                item.complete_without_remote_job(
                    operation=PendingOperation.DELETE_DOCUMENT,
                    content_hash=None,
                    metadata_hash=None,
                    document_id=None,
                    version_id=None,
                    at=now,
                )
                job.complete(worker_id, now)
                await uow.items.update(item)
                await uow.jobs.update(job)
                await uow.commit()
            return False

        result = await self._knowledge_index.delete_document(document_id)
        return await self._finish_or_wait(
            job_id=job_id,
            worker_id=worker_id,
            operation=PendingOperation.DELETE_DOCUMENT,
            content_hash=None,
            metadata_hash=None,
            result=result,
            remote_job_exists=True,
        )

    async def _poll_knowledge_index(self, job_id: UUID, worker_id: str) -> bool:
        now = self._clock()
        async with self._uow_factory() as uow:
            job, item, _ = await _load_job_context(uow, job_id)
            job.assert_lease(worker_id, now)
            remote_job_id = item.knowledge_index_job_id
            operation = item.pending_operation
            if operation is None:
                raise ValidationError("Polling job has no pending Knowledge Index operation")

        if remote_job_id is None:
            if operation == PendingOperation.DELETE_DOCUMENT:
                return await self._delete_item(job_id, worker_id)
            return await self._process_item(job_id, worker_id)

        result = await self._knowledge_index.get_job(remote_job_id)
        now = self._clock()
        async with self._uow_factory() as uow:
            job, item, _ = await _load_job_context(uow, job_id)
            job.assert_lease(worker_id, now)
            if result.status in {
                KnowledgeIndexJobStatus.PENDING,
                KnowledgeIndexJobStatus.PROCESSING,
            }:
                job.defer(worker_id, now, self._poll_interval)
                await uow.jobs.update(job)
                await uow.commit()
                return True
            if result.status == KnowledgeIndexJobStatus.FAILED:
                message = result.error_message or "Knowledge Index job failed"
                item.reset_failed_remote_job(now)
                job.record_failure(
                    worker_id,
                    now,
                    code="knowledge_index_error",
                    message=message,
                    retryable=False,
                    base_delay=self._retry_base_delay,
                )
                item.fail("knowledge_index_error", message, now)
                await uow.items.update(item)
                await uow.jobs.update(job)
                await uow.commit()
                return False
            item.knowledge_index_version_id = result.version_id
            item.complete_pending_operation(now)
            job.complete(worker_id, now)
            await uow.items.update(item)
            await uow.jobs.update(job)
            await uow.commit()
        return False

    async def _assert_source_version(
        self,
        path: Path,
        expected_source_version: str,
        stage: str,
    ) -> None:
        observed_source_version = await asyncio.to_thread(self._source_version_reader, path)
        if observed_source_version != expected_source_version:
            raise SourceChangedError(
                f"Source file changed {stage}; expected {expected_source_version}, "
                f"observed {observed_source_version}"
            )

    async def _finish_or_wait(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        operation: PendingOperation,
        content_hash: str | None,
        metadata_hash: str | None,
        result: KnowledgeIndexJobResult,
        remote_job_exists: bool,
    ) -> bool:
        """統一處理 KIS 同步完成與非同步 pending 兩種 contract。"""

        now = self._clock()
        if result.status == KnowledgeIndexJobStatus.FAILED:
            raise ExternalServiceError(
                result.error_message or "Knowledge Index job failed",
                retryable=False,
            )
        async with self._uow_factory() as uow:
            job, item, _ = await _load_job_context(uow, job_id)
            job.assert_lease(worker_id, now)
            if result.status == KnowledgeIndexJobStatus.COMPLETED:
                item.complete_without_remote_job(
                    operation=operation,
                    content_hash=content_hash,
                    metadata_hash=metadata_hash,
                    document_id=result.document_id,
                    version_id=result.version_id,
                    at=now,
                )
                job.complete(worker_id, now)
                await uow.items.update(item)
                await uow.jobs.update(job)
                await uow.commit()
                return False
            if not remote_job_exists:
                raise ExternalServiceError(
                    "Knowledge Index returned pending status without a job ID",
                    retryable=True,
                )
            item.wait_for_index(
                operation=operation,
                content_hash=content_hash,
                metadata_hash=metadata_hash,
                document_id=result.document_id,
                version_id=result.version_id,
                job_id=result.job_id,
                at=now,
            )
            # poll key 包含遠端 job ID，同一個 pending job 只會建立一筆輪詢工作。
            poll_key = f"poll:{item.id}:{result.job_id}"
            await _enqueue_job(
                uow,
                source_id=item.source_id,
                item=item,
                run_id=job.sync_run_id,
                job_type=IngestionJobType.POLL_KNOWLEDGE_INDEX,
                idempotency_key=poll_key,
                max_attempts=self._max_job_attempts,
                now=now,
            )
            job.complete(worker_id, now)
            await uow.items.update(item)
            await uow.jobs.update(job)
            await uow.commit()
        return False

    async def _record_permanent_failure(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        item_status: SourceItemStatus,
        code: str,
        message: str,
    ) -> ProcessJobResult:
        now = self._clock()
        async with self._uow_factory() as uow:
            job, item, _ = await _load_job_context(uow, job_id)
            job.record_failure(
                worker_id,
                now,
                code=code,
                message=message,
                retryable=False,
                base_delay=self._retry_base_delay,
            )
            item.quarantine(item_status, code, message, now)
            await uow.items.update(item)
            await uow.jobs.update(job)
            await uow.commit()
            return _process_result(job, item)

    async def _record_retryable_failure(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> ProcessJobResult:
        now = self._clock()
        async with self._uow_factory() as uow:
            job, item, _ = await _load_job_context(uow, job_id)
            job.record_failure(
                worker_id,
                now,
                code=code,
                message=message,
                retryable=retryable,
                base_delay=self._retry_base_delay,
            )
            item.fail(code, message, now)
            await uow.items.update(item)
            await uow.jobs.update(job)
            await uow.commit()
            return _process_result(job, item)


class RetryIngestionJob:
    """由管理者手動把最終失敗的 job 重設為 pending。"""

    def __init__(self, uow_factory: UnitOfWorkFactory, *, clock: Clock = utc_now) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(self, job_id: UUID) -> IngestionJobView:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get(job_id)
            if job is None:
                raise NotFoundError(f"Job {job_id} was not found")
            item = await uow.items.get(job.item_id)
            if item is None:
                raise NotFoundError(f"Source item {job.item_id} was not found")
            now = self._clock()
            job.retry_manually(now)
            if item.status != SourceItemStatus.DELETED:
                item.begin(SourceItemStatus.PENDING, now)
            await uow.items.update(item)
            await uow.jobs.update(job)
            await uow.commit()
            return _job_view(job)


class GetSourceItem:
    """依 ID 查詢一筆 source item，回傳不可變 view。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, item_id: UUID) -> SourceItemView:
        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise NotFoundError(f"Source item {item_id} was not found")
            return _item_view(item)


class ListSourceItems:
    """依 source/status 分頁列出 source items。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self,
        *,
        source_id: UUID | None = None,
        status: SourceItemStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[SourceItemView, ...]:
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValidationError("Invalid source item pagination")
        async with self._uow_factory() as uow:
            items = await uow.items.list_items(
                source_id=source_id,
                status=status,
                offset=offset,
                limit=limit,
            )
            return tuple(_item_view(item) for item in items)


class GetIngestionJob:
    """查詢 ingestion job 狀態。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, job_id: UUID) -> IngestionJobView:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get(job_id)
            if job is None:
                raise NotFoundError(f"Job {job_id} was not found")
            return _job_view(job)


class GetSyncRun:
    """查詢 scan run 統計。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, run_id: UUID) -> SyncRunView:
        async with self._uow_factory() as uow:
            run = await uow.sync_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"Sync run {run_id} was not found")
            return SyncRunView(
                id=run.id,
                source_id=run.source_id,
                status=run.status,
                discovered_count=run.discovered_count,
                queued_count=run.queued_count,
                missing_count=run.missing_count,
                delete_queued_count=run.delete_queued_count,
                error_message=run.error_message,
                started_at=run.started_at,
                completed_at=run.completed_at,
            )


async def _load_job_context(
    uow: UnitOfWork, job_id: UUID
) -> tuple[IngestionJob, SourceItem, Source]:
    """一次載入 job、item、source；缺任何一筆都轉成一致的 NotFoundError。"""

    job = await uow.jobs.get(job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} was not found")
    item = await uow.items.get(job.item_id)
    if item is None:
        raise NotFoundError(f"Source item {job.item_id} was not found")
    source = await uow.sources.get(item.source_id)
    if source is None:
        raise NotFoundError(f"Source {item.source_id} was not found")
    return job, item, source


async def _enqueue_job(
    uow: UnitOfWork,
    *,
    source_id: UUID,
    item: SourceItem,
    run_id: UUID | None,
    job_type: IngestionJobType,
    idempotency_key: str,
    max_attempts: int,
    now: datetime,
) -> bool:
    """以 idempotency key 建立 job；已存在回傳 False 而不是重複排程。"""

    existing = await uow.jobs.get_by_idempotency_key(idempotency_key)
    if existing is not None:
        return False
    job = IngestionJob(
        id=uuid5(NAMESPACE_URL, f"ingestion-job:{idempotency_key}"),
        source_id=source_id,
        item_id=item.id,
        job_type=job_type,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        sync_run_id=run_id,
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    await uow.jobs.add(job)
    return True


def _process_job_key(item: SourceItem) -> str:
    return f"process:{item.id}:{item.discovery_fingerprint}:{item.lifecycle_generation}"


def _validated_relative_source_item_id(value: str) -> str:
    """只接受標準相對 POSIX path，阻擋絕對路徑與 ``..`` path traversal。"""

    stripped = value.strip()
    path = PurePosixPath(stripped)
    if (
        not stripped
        or path.is_absolute()
        or "\\" in stripped
        or ".." in path.parts
        or path.as_posix() != stripped
    ):
        raise ValidationError("source_item_id must be a normalized relative POSIX path")
    return stripped


def _knowledge_index_source_item_id(item: SourceItem) -> str:
    """建立不洩漏真實路徑、但同一文件保持穩定的 KIS source_item_id。"""

    path_hash = hashlib.sha256(item.source_item_id.encode("utf-8")).hexdigest()
    return f"local:{item.source_id}:{path_hash}"


def _discovered_from_item(item: SourceItem) -> DiscoveredFile:
    return DiscoveredFile(
        source_item_id=item.source_item_id,
        relative_path=item.source_path,
        absolute_path=Path(item.absolute_path),
        file_size=item.file_size,
        modified_at=item.modified_at,
    )


def _source_version_for_path(path: Path) -> str:
    try:
        file_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SourceChangedError(f"Source file is unavailable: {path}") from exc
    if not stat_module.S_ISREG(file_stat.st_mode):
        raise SourceChangedError(f"Source path is no longer a regular file: {path}")
    modified_at = datetime.fromtimestamp(file_stat.st_mtime, tz=UTC)
    return f"mtime:{modified_at.isoformat()};size:{file_stat.st_size}"


def _content_hash(parser_name: str, parser_version: str, chunks: Sequence[Chunk]) -> str:
    """對 parser 版本與 chunk 結果做 canonical JSON hash，偵測實際索引內容變化。"""

    payload = {
        "parser_name": parser_name,
        "parser_version": parser_version,
        "chunks": [
            {
                "content": chunk.content,
                "section_path": chunk.section_path,
                "page_number": chunk.page_number,
                "token_count": chunk.token_count,
            }
            for chunk in chunks
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _metadata_state_hash(metadata: DocumentMetadata, source_version: str) -> str:
    """結合來源版本與 metadata fingerprint，供 metadata-only update 判斷。"""

    encoded = f"{source_version}\0{metadata.fingerprint()}".encode()
    return hashlib.sha256(encoded).hexdigest()


def _submission(
    item: SourceItem,
    metadata: DocumentMetadata,
    parsed: ParsedDocument,
    chunks: Sequence[Chunk],
    content_hash: str,
) -> KnowledgeIndexDocumentSubmission:
    return KnowledgeIndexDocumentSubmission(
        source_type="local_folder",
        source_item_id=_knowledge_index_source_item_id(item),
        source_version=item.source_version,
        title=metadata.title,
        document_type=metadata.document_type,
        owner_department=metadata.owner_department,
        document_owner=metadata.document_owner,
        confidentiality=metadata.confidentiality,
        status=metadata.status,
        language=metadata.language,
        external_llm_allowed=metadata.external_llm_allowed,
        version_number=metadata.version_number or item.source_version,
        content_hash=content_hash,
        mime_type=parsed.mime_type,
        file_size=item.file_size,
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        chunks=tuple(chunks),
        source_path=item.source_path,
        source_url=metadata.source_url,
        effective_at=metadata.effective_at,
        expires_at=metadata.expires_at,
        review_at=metadata.review_at,
        custom_metadata=metadata.custom_metadata,
        acl=metadata.acl,
    )


def _metadata_patch(item: SourceItem, metadata: DocumentMetadata) -> KnowledgeIndexMetadataPatch:
    return KnowledgeIndexMetadataPatch(
        source_version=item.source_version,
        version_number=metadata.version_number or item.source_version,
        title=metadata.title,
        document_type=metadata.document_type,
        owner_department=metadata.owner_department,
        document_owner=metadata.document_owner,
        confidentiality=metadata.confidentiality,
        status=metadata.status,
        language=metadata.language,
        external_llm_allowed=metadata.external_llm_allowed,
        source_path=item.source_path,
        source_url=metadata.source_url,
        effective_at=metadata.effective_at,
        expires_at=metadata.expires_at,
        review_at=metadata.review_at,
        custom_metadata=metadata.custom_metadata,
        acl=metadata.acl,
    )


def _job_view(job: IngestionJob) -> IngestionJobView:
    return IngestionJobView(
        id=job.id,
        source_id=job.source_id,
        item_id=job.item_id,
        job_type=job.job_type,
        status=job.status,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        available_at=job.available_at,
        leased_by=job.leased_by,
        lease_expires_at=job.lease_expires_at,
        error_code=job.error_code,
        error_message=job.error_message,
        updated_at=job.updated_at,
    )


def _item_view(item: SourceItem) -> SourceItemView:
    return SourceItemView(
        id=item.id,
        source_id=item.source_id,
        source_item_id=item.source_item_id,
        source_path=item.source_path,
        source_version=item.source_version,
        mime_type=item.mime_type,
        file_size=item.file_size,
        status=item.status,
        missing_scan_count=item.missing_scan_count,
        knowledge_index_document_id=item.knowledge_index_document_id,
        knowledge_index_version_id=item.knowledge_index_version_id,
        knowledge_index_job_id=item.knowledge_index_job_id,
        error_code=item.error_code,
        error_message=item.error_message,
        updated_at=item.updated_at,
    )


def _process_result(job: IngestionJob, item: SourceItem) -> ProcessJobResult:
    return ProcessJobResult(
        job_id=job.id,
        item_id=item.id,
        job_type=job.job_type,
        status=job.status,
        item_status=item.status,
        error_code=job.error_code,
        error_message=job.error_message,
    )

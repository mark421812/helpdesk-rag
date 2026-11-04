from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from uuid import UUID, uuid4

from ingestion_service.application.dtos import (
    KnowledgeIndexDocumentSubmission,
    KnowledgeIndexJobResult,
    KnowledgeIndexMetadataPatch,
    KnowledgeIndexSubmissionResult,
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
    SyncRun,
)
from ingestion_service.domain.enums import (
    IngestionJobStatus,
    KnowledgeIndexingStatus,
    KnowledgeIndexJobStatus,
    SourceItemStatus,
    SourceType,
)


class FakeSourceRepository:
    def __init__(self) -> None:
        self.values: dict[UUID, Source] = {}

    async def get(self, source_id: UUID) -> Source | None:
        return self.values.get(source_id)

    async def add(self, source: Source) -> None:
        self.values[source.id] = source

    async def update(self, source: Source) -> None:
        self.values[source.id] = source

    async def list_enabled(self, source_type: SourceType | None = None) -> Sequence[Source]:
        return tuple(
            value
            for value in self.values.values()
            if value.enabled and (source_type is None or value.source_type == source_type)
        )


class FakeSourceItemRepository:
    def __init__(self) -> None:
        self.values: dict[UUID, SourceItem] = {}

    async def get(self, item_id: UUID) -> SourceItem | None:
        return self.values.get(item_id)

    async def find_by_source_item_id(
        self, source_id: UUID, source_item_id: str
    ) -> SourceItem | None:
        return next(
            (
                item
                for item in self.values.values()
                if item.source_id == source_id and item.source_item_id == source_item_id
            ),
            None,
        )

    async def add(self, item: SourceItem) -> None:
        self.values[item.id] = item

    async def update(self, item: SourceItem) -> None:
        self.values[item.id] = item

    async def list_for_source(self, source_id: UUID) -> Sequence[SourceItem]:
        return tuple(item for item in self.values.values() if item.source_id == source_id)

    async def list_items(
        self,
        *,
        source_id: UUID | None = None,
        status: SourceItemStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Sequence[SourceItem]:
        values = [
            item
            for item in self.values.values()
            if (source_id is None or item.source_id == source_id)
            and (status is None or item.status == status)
        ]
        return tuple(values[offset : offset + limit])


class FakeSyncRunRepository:
    def __init__(self) -> None:
        self.values: dict[UUID, SyncRun] = {}

    async def get(self, run_id: UUID) -> SyncRun | None:
        return self.values.get(run_id)

    async def add(self, run: SyncRun) -> None:
        self.values[run.id] = run

    async def update(self, run: SyncRun) -> None:
        self.values[run.id] = run


class FakeJobRepository:
    def __init__(self) -> None:
        self.values: dict[UUID, IngestionJob] = {}

    async def get(self, job_id: UUID) -> IngestionJob | None:
        return self.values.get(job_id)

    async def get_by_idempotency_key(self, idempotency_key: str) -> IngestionJob | None:
        return next(
            (job for job in self.values.values() if job.idempotency_key == idempotency_key),
            None,
        )

    async def add(self, job: IngestionJob) -> None:
        self.values[job.id] = job

    async def update(self, job: IngestionJob) -> None:
        self.values[job.id] = job

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> IngestionJob | None:
        eligible = sorted(
            (
                job
                for job in self.values.values()
                if job.available_at <= now
                and (
                    job.status == IngestionJobStatus.PENDING
                    or (
                        job.status == IngestionJobStatus.PROCESSING
                        and job.lease_expires_at is not None
                        and job.lease_expires_at <= now
                    )
                )
            ),
            key=lambda job: (job.available_at, job.created_at),
        )
        if not eligible:
            return None
        eligible[0].claim(worker_id, now, lease_for)
        return eligible[0]


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.sources = FakeSourceRepository()
        self.items = FakeSourceItemRepository()
        self.sync_runs = FakeSyncRunRepository()
        self.jobs = FakeJobRepository()
        self.commits = 0
        self.rollbacks = 0

    def __call__(self) -> FakeUnitOfWork:
        return self

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rollbacks += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeScanner:
    def __init__(self, files: Sequence[DiscoveredFile] = ()) -> None:
        self.files = tuple(files)
        self.error: Exception | None = None

    def scan(self, source: Source) -> Sequence[DiscoveredFile]:
        del source
        if self.error is not None:
            raise self.error
        return self.files

    def discover(self, source: Source, source_item_id: str) -> DiscoveredFile | None:
        del source
        if self.error is not None:
            raise self.error
        return next(
            (file for file in self.files if file.source_item_id == source_item_id),
            None,
        )


class FakeMetadataLoader:
    def __init__(self, metadata: DocumentMetadata) -> None:
        self.metadata = metadata
        self.source_hash = "1" * 64
        self.error: Exception | None = None

    def source_fingerprint(self, source: Source, discovered: DiscoveredFile) -> str:
        del source, discovered
        return self.source_hash

    def load(
        self,
        source: Source,
        discovered: DiscoveredFile,
        parsed: ParsedDocument,
    ) -> DocumentMetadata:
        del source, discovered, parsed
        if self.error is not None:
            raise self.error
        return self.metadata


class FakeParser:
    def __init__(self) -> None:
        self.document = ParsedDocument(
            blocks=(ParsedBlock("Employee leave is approved by the manager."),),
            parser_name="fake",
            parser_version="1",
            mime_type="text/plain",
        )
        self.error: Exception | None = None
        self.on_parse: Callable[[Path], None] | None = None

    def parse(self, path: Path) -> ParsedDocument:
        if self.error is not None:
            raise self.error
        if self.on_parse is not None:
            self.on_parse(path)
        return self.document


class FakeParserRegistry:
    def __init__(self, parser: FakeParser) -> None:
        self.parser = parser

    def get_parser(self, path: Path, mime_type: str | None = None) -> FakeParser:
        del path, mime_type
        return self.parser


class FakeChunker:
    def __init__(self) -> None:
        self.chunks = (
            Chunk(
                content="Employee leave is approved by the manager.",
                section_path="Leave",
                page_number=1,
                token_count=10,
            ),
        )

    def chunk(self, document: ParsedDocument) -> tuple[Chunk, ...]:
        del document
        return self.chunks


class FakeKnowledgeIndexClient:
    def __init__(self) -> None:
        self.document_id = uuid4()
        self.version_id = uuid4()
        self.remote_job_id = uuid4()
        self.submit_status = KnowledgeIndexingStatus.INDEXED
        self.patch_status = KnowledgeIndexJobStatus.COMPLETED
        self.delete_status = KnowledgeIndexJobStatus.COMPLETED
        self.submit_error: Exception | None = None
        self.submit_calls: list[KnowledgeIndexDocumentSubmission] = []
        self.patch_calls: list[tuple[UUID, KnowledgeIndexMetadataPatch]] = []
        self.delete_calls: list[UUID] = []
        self.poll_results: list[KnowledgeIndexJobResult] = []

    async def submit_document(
        self, submission: KnowledgeIndexDocumentSubmission
    ) -> KnowledgeIndexSubmissionResult:
        if self.submit_error is not None:
            raise self.submit_error
        self.submit_calls.append(submission)
        return KnowledgeIndexSubmissionResult(
            document_id=self.document_id,
            version_id=self.version_id,
            job_id=(
                None
                if self.submit_status == KnowledgeIndexingStatus.INDEXED
                else self.remote_job_id
            ),
            indexing_status=self.submit_status,
            created=True,
        )

    async def patch_metadata(
        self, document_id: UUID, patch: KnowledgeIndexMetadataPatch
    ) -> KnowledgeIndexJobResult:
        self.patch_calls.append((document_id, patch))
        return self._job_result(self.patch_status)

    async def delete_document(self, document_id: UUID) -> KnowledgeIndexJobResult:
        self.delete_calls.append(document_id)
        return self._job_result(self.delete_status)

    async def get_job(self, job_id: UUID) -> KnowledgeIndexJobResult:
        del job_id
        if self.poll_results:
            return self.poll_results.pop(0)
        return self._job_result(KnowledgeIndexJobStatus.COMPLETED)

    def _job_result(self, status: KnowledgeIndexJobStatus) -> KnowledgeIndexJobResult:
        return KnowledgeIndexJobResult(
            job_id=self.remote_job_id,
            document_id=self.document_id,
            version_id=self.version_id,
            status=status,
        )

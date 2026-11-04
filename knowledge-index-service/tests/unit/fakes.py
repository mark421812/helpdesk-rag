from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from knowledge_index_service.domain.entities import (
    AccessControlEntry,
    Chunk,
    Document,
    DocumentVersion,
    IndexingJob,
    OutboxEvent,
    SearchFilters,
    VectorPoint,
    VectorSearchHit,
)
from knowledge_index_service.domain.enums import (
    EmbeddingInputType,
    IndexingStatus,
    JobStatus,
    JobType,
    OutboxStatus,
)
from knowledge_index_service.domain.model_info import ModelInfo


@dataclass
class MemoryStore:
    documents: dict[UUID, Document] = field(default_factory=dict)
    versions: dict[UUID, DocumentVersion] = field(default_factory=dict)
    chunks: dict[UUID, Chunk] = field(default_factory=dict)
    jobs: dict[UUID, IndexingJob] = field(default_factory=dict)
    events: dict[UUID, OutboxEvent] = field(default_factory=dict)
    document_get_calls: int = 0
    document_get_many_calls: int = 0
    version_get_calls: int = 0
    version_get_many_calls: int = 0


class FakeDocumentRepository:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def get(self, document_id: UUID) -> Document | None:
        self._store.document_get_calls += 1
        return self._store.documents.get(document_id)

    async def get_many(self, document_ids: Sequence[UUID]) -> list[Document]:
        self._store.document_get_many_calls += 1
        return [
            self._store.documents[document_id]
            for document_id in dict.fromkeys(document_ids)
            if document_id in self._store.documents
        ]

    async def get_for_update(self, document_id: UUID) -> Document | None:
        return self._store.documents.get(document_id)

    async def find_by_source(self, source_type: str, source_item_id: str) -> Document | None:
        return next(
            (
                document
                for document in self._store.documents.values()
                if document.source_type == source_type and document.source_item_id == source_item_id
            ),
            None,
        )

    async def add(self, document: Document) -> None:
        self._store.documents[document.id] = document

    async def update(self, document: Document) -> None:
        self._store.documents[document.id] = document

    async def replace_acl(
        self,
        document_id: UUID,
        entries: Sequence[AccessControlEntry],
    ) -> None:
        document = self._store.documents[document_id]
        document.acl = list(entries)

    async def get_version(self, version_id: UUID) -> DocumentVersion | None:
        self._store.version_get_calls += 1
        return self._store.versions.get(version_id)

    async def get_versions(self, version_ids: Sequence[UUID]) -> list[DocumentVersion]:
        self._store.version_get_many_calls += 1
        return [
            self._store.versions[version_id]
            for version_id in dict.fromkeys(version_ids)
            if version_id in self._store.versions
        ]

    async def find_version_by_hash(
        self,
        document_id: UUID,
        content_hash: str,
        embedding_model: str,
        embedding_model_revision: str,
    ) -> DocumentVersion | None:
        return next(
            (
                version
                for version in self._store.versions.values()
                if version.document_id == document_id
                and version.content_hash == content_hash
                and version.embedding_model == embedding_model
                and version.embedding_model_revision == embedding_model_revision
            ),
            None,
        )

    async def add_version(self, version: DocumentVersion) -> None:
        self._store.versions[version.id] = version

    async def update_version_metadata(self, version: DocumentVersion) -> None:
        self._store.versions[version.id] = version

    async def update_version_status(
        self,
        version_id: UUID,
        status: IndexingStatus,
        indexed_at: datetime | None = None,
    ) -> None:
        version = self._store.versions[version_id]
        version.indexing_status = status
        version.indexed_at = indexed_at

    async def set_current_version(self, document_id: UUID, version_id: UUID) -> None:
        self._store.documents[document_id].current_version_id = version_id

    async def add_chunks(self, chunks: Sequence[Chunk]) -> None:
        self._store.chunks.update({chunk.id: chunk for chunk in chunks})

    async def list_chunks(self, version_id: UUID) -> list[Chunk]:
        return sorted(
            (chunk for chunk in self._store.chunks.values() if chunk.version_id == version_id),
            key=lambda chunk: chunk.chunk_index,
        )

    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]:
        return [
            self._store.chunks[chunk_id] for chunk_id in chunk_ids if chunk_id in self._store.chunks
        ]

    async def update_chunks_status(
        self,
        version_id: UUID,
        status: IndexingStatus,
        point_ids: dict[UUID, str] | None = None,
    ) -> None:
        for chunk in self._store.chunks.values():
            if chunk.version_id == version_id:
                chunk.indexing_status = status
                if point_ids:
                    chunk.qdrant_point_id = point_ids.get(chunk.id)


class FakeJobRepository:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def get(self, job_id: UUID) -> IndexingJob | None:
        return self._store.jobs.get(job_id)

    async def add(self, job: IndexingJob) -> None:
        self._store.jobs[job.id] = job

    async def find_active_for_version(
        self,
        document_id: UUID,
        version_id: UUID | None,
    ) -> IndexingJob | None:
        return next(
            (
                job
                for job in reversed(self._store.jobs.values())
                if job.document_id == document_id
                and job.version_id == version_id
                and job.job_type == JobType.INDEX_VERSION
                and job.status in {JobStatus.PENDING, JobStatus.PROCESSING}
            ),
            None,
        )

    async def find_active_for_document(
        self,
        document_id: UUID,
        job_type: JobType,
    ) -> IndexingJob | None:
        return next(
            (
                job
                for job in reversed(self._store.jobs.values())
                if job.document_id == document_id
                and job.job_type == job_type
                and job.status in {JobStatus.PENDING, JobStatus.PROCESSING}
            ),
            None,
        )

    async def find_latest_for_document(
        self,
        document_id: UUID,
        job_type: JobType,
    ) -> IndexingJob | None:
        return next(
            (
                job
                for job in reversed(self._store.jobs.values())
                if job.document_id == document_id and job.job_type == job_type
            ),
            None,
        )

    async def mark_processing(self, job_id: UUID, started_at: datetime) -> None:
        job = self._store.jobs[job_id]
        job.status = JobStatus.PROCESSING
        job.started_at = started_at

    async def mark_completed(self, job_id: UUID, completed_at: datetime) -> None:
        job = self._store.jobs[job_id]
        job.status = JobStatus.COMPLETED
        job.completed_at = completed_at

    async def mark_failed(
        self,
        job_id: UUID,
        error_code: str,
        error_message: str,
        completed_at: datetime,
    ) -> None:
        job = self._store.jobs[job_id]
        job.status = JobStatus.FAILED
        job.error_code = error_code
        job.error_message = error_message
        job.completed_at = completed_at

    async def increment_attempt(self, job_id: UUID) -> int:
        job = self._store.jobs[job_id]
        job.attempt_count += 1
        return job.attempt_count


class FakeOutboxRepository:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def add(self, event: OutboxEvent) -> None:
        self._store.events[event.id] = event

    async def claim_next(
        self,
        now: datetime,
        lease_expired_before: datetime,
    ) -> OutboxEvent | None:
        for event in self._store.events.values():
            if (event.available_at <= now and event.status == OutboxStatus.PENDING) or (
                event.status == OutboxStatus.PROCESSING
                and event.locked_at is not None
                and event.locked_at < lease_expired_before
            ):
                event.status = OutboxStatus.PROCESSING
                event.locked_at = now
                event.attempt_count += 1
                return event
        return None

    async def mark_processed(self, event_id: UUID, processed_at: datetime) -> None:
        event = self._store.events[event_id]
        event.status = OutboxStatus.PROCESSED
        event.processed_at = processed_at

    async def reschedule(
        self,
        event_id: UUID,
        available_at: datetime,
        error_message: str,
    ) -> None:
        event = self._store.events[event_id]
        event.status = OutboxStatus.PENDING
        event.available_at = available_at
        event.last_error = error_message

    async def mark_failed(self, event_id: UUID, error_message: str) -> None:
        event = self._store.events[event_id]
        event.status = OutboxStatus.FAILED
        event.last_error = error_message


class FakeUnitOfWork:
    def __init__(self, store: MemoryStore) -> None:
        self.documents = FakeDocumentRepository(store)
        self.jobs = FakeJobRepository(store)
        self.outbox = FakeOutboxRepository(store)
        self.committed = False

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc:
            await self.rollback()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.committed = False


class FakeUnitOfWorkFactory:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def __call__(self) -> FakeUnitOfWork:
        return FakeUnitOfWork(self.store)


class FakeEmbeddingModel:
    def __init__(self, dimension: int = 3, revision: str = "test") -> None:
        self.dimension = dimension
        self.revision = revision
        self.calls: list[tuple[tuple[str, ...], EmbeddingInputType]] = []

    async def model_info(self) -> ModelInfo:
        return ModelInfo(
            model_id="fake-e5",
            revision=self.revision,
            dimension=self.dimension,
            max_sequence_length=512,
            normalized=True,
            device="cpu",
        )

    async def embed(
        self,
        texts: Sequence[str],
        input_type: EmbeddingInputType,
    ) -> list[list[float]]:
        self.calls.append((tuple(texts), input_type))
        return [
            [float(index + 1) / self.dimension for index in range(self.dimension)] for _ in texts
        ]

    async def is_ready(self) -> bool:
        return True


class FakeVectorStore:
    def __init__(self) -> None:
        self.points: dict[str, VectorPoint] = {}
        self.synced_documents: list[UUID] = []
        self.deleted_documents: list[UUID] = []
        self.dimension: int | None = None
        self.hits: list[VectorSearchHit] = []
        self.search_calls: list[tuple[int, int, SearchFilters, float | None]] = []

    async def ensure_collection(self, dimension: int) -> None:
        self.dimension = dimension

    async def upsert(self, points: Sequence[VectorPoint]) -> None:
        self.points.update({point.point_id: point for point in points})

    async def search(
        self,
        vector: Sequence[float],
        limit: int,
        filters: SearchFilters,
        score_threshold: float | None,
        offset: int = 0,
    ) -> list[VectorSearchHit]:
        del vector
        self.search_calls.append((limit, offset, filters, score_threshold))
        return self.hits[offset : offset + limit]

    async def deactivate_document_versions(
        self,
        document_id: UUID,
        active_version_id: UUID,
    ) -> None:
        del document_id, active_version_id

    async def sync_document_payload(self, document: Document) -> None:
        self.synced_documents.append(document.id)

    async def delete_document(self, document_id: UUID) -> None:
        self.deleted_documents.append(document_id)
        self.points = {
            point_id: point
            for point_id, point in self.points.items()
            if point.payload.get("document_id") != str(document_id)
        }

    async def is_ready(self) -> bool:
        return True

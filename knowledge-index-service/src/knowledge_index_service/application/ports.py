"""Application ports：KIS 核心需要、但不自行實作的外部能力。

``Protocol`` 只描述方法形狀。Use case 依賴這些抽象介面；Infrastructure 再提供 PostgreSQL、
Qdrant 與 embedding model 實作，測試則可注入 in-memory fakes。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol
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
from knowledge_index_service.domain.enums import EmbeddingInputType, IndexingStatus, JobType
from knowledge_index_service.domain.model_info import ModelInfo


class EmbeddingModelPort(Protocol):
    """取得模型資訊、產生 embedding 與 readiness 的介面。"""

    async def model_info(self) -> ModelInfo: ...

    async def embed(
        self,
        texts: Sequence[str],
        input_type: EmbeddingInputType,
    ) -> list[list[float]]: ...

    async def is_ready(self) -> bool: ...


class VectorStorePort(Protocol):
    """Application 對 vector index 所需的最小操作集合。"""

    async def ensure_collection(self, dimension: int) -> None: ...

    async def upsert(self, points: Sequence[VectorPoint]) -> None: ...

    async def search(
        self,
        vector: Sequence[float],
        limit: int,
        filters: SearchFilters,
        score_threshold: float | None,
        offset: int = 0,
    ) -> list[VectorSearchHit]: ...

    async def deactivate_document_versions(
        self,
        document_id: UUID,
        active_version_id: UUID,
    ) -> None: ...

    async def sync_document_payload(self, document: Document) -> None: ...

    async def delete_document(self, document_id: UUID) -> None: ...

    async def is_ready(self) -> bool: ...


class DocumentRepository(Protocol):
    """文件 aggregate、版本、chunk 與 ACL 的 persistence port。"""

    async def get(self, document_id: UUID) -> Document | None: ...

    async def get_many(self, document_ids: Sequence[UUID]) -> list[Document]: ...

    async def get_for_update(self, document_id: UUID) -> Document | None: ...

    async def find_by_source(self, source_type: str, source_item_id: str) -> Document | None: ...

    async def add(self, document: Document) -> None: ...

    async def update(self, document: Document) -> None: ...

    async def replace_acl(
        self,
        document_id: UUID,
        entries: Sequence[AccessControlEntry],
    ) -> None: ...

    async def get_version(self, version_id: UUID) -> DocumentVersion | None: ...

    async def get_versions(self, version_ids: Sequence[UUID]) -> list[DocumentVersion]: ...

    async def find_version_by_hash(
        self,
        document_id: UUID,
        content_hash: str,
        embedding_model: str,
        embedding_model_revision: str,
    ) -> DocumentVersion | None: ...

    async def add_version(self, version: DocumentVersion) -> None: ...

    async def update_version_metadata(self, version: DocumentVersion) -> None: ...

    async def update_version_status(
        self,
        version_id: UUID,
        status: IndexingStatus,
        indexed_at: datetime | None = None,
    ) -> None: ...

    async def set_current_version(self, document_id: UUID, version_id: UUID) -> None: ...

    async def add_chunks(self, chunks: Sequence[Chunk]) -> None: ...

    async def list_chunks(self, version_id: UUID) -> list[Chunk]: ...

    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]: ...

    async def update_chunks_status(
        self,
        version_id: UUID,
        status: IndexingStatus,
        point_ids: dict[UUID, str] | None = None,
    ) -> None: ...


class JobRepository(Protocol):
    """索引工作狀態與 retry attempt 的 persistence port。"""

    async def get(self, job_id: UUID) -> IndexingJob | None: ...

    async def add(self, job: IndexingJob) -> None: ...

    async def find_active_for_version(
        self,
        document_id: UUID,
        version_id: UUID | None,
    ) -> IndexingJob | None: ...

    async def find_active_for_document(
        self,
        document_id: UUID,
        job_type: JobType,
    ) -> IndexingJob | None: ...

    async def find_latest_for_document(
        self,
        document_id: UUID,
        job_type: JobType,
    ) -> IndexingJob | None: ...

    async def mark_processing(self, job_id: UUID, started_at: datetime) -> None: ...

    async def mark_completed(self, job_id: UUID, completed_at: datetime) -> None: ...

    async def mark_failed(
        self,
        job_id: UUID,
        error_code: str,
        error_message: str,
        completed_at: datetime,
    ) -> None: ...

    async def increment_attempt(self, job_id: UUID) -> int: ...


class OutboxRepository(Protocol):
    """durable outbox event 的 claim／完成／重排介面。"""

    async def add(self, event: OutboxEvent) -> None: ...

    async def claim_next(
        self,
        now: datetime,
        lease_expired_before: datetime,
    ) -> OutboxEvent | None: ...

    async def mark_processed(self, event_id: UUID, processed_at: datetime) -> None: ...

    async def reschedule(
        self,
        event_id: UUID,
        available_at: datetime,
        error_message: str,
    ) -> None: ...

    async def mark_failed(self, event_id: UUID, error_message: str) -> None: ...


class UnitOfWork(Protocol):
    """把三個 repositories 與 transaction boundary 綁在一起。

    ``async with uow`` 會呼叫 ``__aenter__``/``__aexit__``；若 block 中拋出例外，實作會
    rollback。這是 Python async context manager protocol。
    """

    documents: DocumentRepository
    jobs: JobRepository
    outbox: OutboxRepository

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    """每次呼叫都建立新的 UnitOfWork/DB session，不能跨 request 共用 transaction。"""

    def __call__(self) -> UnitOfWork: ...

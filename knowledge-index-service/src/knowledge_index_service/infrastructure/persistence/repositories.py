"""以 SQLAlchemy 實作 KIS repository ports。

Repository 封裝 SQL 查詢與 ORM/Domain mapping；Application use case 因此只需要呼叫
``get``、``add``、``update`` 等語意方法，不必知道 table 或 SQLAlchemy API。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_index_service.domain.entities import (
    AccessControlEntry,
    Chunk,
    Document,
    DocumentVersion,
    IndexingJob,
    OutboxEvent,
)
from knowledge_index_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    IndexingStatus,
    JobStatus,
    JobType,
    OutboxStatus,
    PermissionEffect,
    PrincipalType,
)
from knowledge_index_service.infrastructure.persistence.models import (
    ChunkModel,
    DocumentAclModel,
    DocumentModel,
    DocumentVersionModel,
    IndexingJobModel,
    OutboxEventModel,
)


class SqlAlchemyDocumentRepository:
    """以 SQLAlchemy 實作 DocumentRepository，並負責 ORM/Domain mapping。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, document_id: UUID) -> Document | None:
        row = await self._session.get(DocumentModel, document_id)
        return await self._to_entity(row) if row else None

    async def get_many(self, document_ids: Sequence[UUID]) -> list[Document]:
        """批次讀取文件與 ACL，避免搜尋結果逐筆查詢造成 N+1。"""

        unique_ids = tuple(dict.fromkeys(document_ids))
        if not unique_ids:
            return []
        rows = (
            await self._session.scalars(
                select(DocumentModel).where(DocumentModel.id.in_(unique_ids))
            )
        ).all()
        if not rows:
            return []
        found_ids = [row.id for row in rows]
        acl_rows = (
            await self._session.scalars(
                select(DocumentAclModel).where(DocumentAclModel.document_id.in_(found_ids))
            )
        ).all()
        acl_by_document: dict[UUID, list[DocumentAclModel]] = {}
        for acl in acl_rows:
            acl_by_document.setdefault(acl.document_id, []).append(acl)
        return [_document_entity(row, acl_by_document.get(row.id, ())) for row in rows]

    async def get_for_update(self, document_id: UUID) -> Document | None:
        row = await self._session.scalar(
            select(DocumentModel).where(DocumentModel.id == document_id).with_for_update()
        )
        return await self._to_entity(row) if row else None

    async def find_by_source(self, source_type: str, source_item_id: str) -> Document | None:
        row = await self._session.scalar(
            select(DocumentModel)
            .where(
                DocumentModel.source_type == source_type,
                DocumentModel.source_item_id == source_item_id,
            )
            # Serialize submit/restore decisions for an existing source identity.
            # This prevents concurrent restore requests from both observing the
            # same tombstone and creating duplicate recovery jobs.
            .with_for_update()
        )
        return await self._to_entity(row) if row else None

    async def add(self, document: Document) -> None:
        self._session.add(_document_model(document))
        await self._session.flush()
        await self.replace_acl(document.id, document.acl)

    async def update(self, document: Document) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(DocumentModel)
                .where(DocumentModel.id == document.id)
                .values(
                    title=document.title,
                    document_type=document.document_type,
                    owner_department=document.owner_department,
                    document_owner=document.document_owner,
                    confidentiality=document.confidentiality.value,
                    status=document.status.value,
                    language=document.language,
                    external_llm_allowed=document.external_llm_allowed,
                    source_path=document.source_path,
                    source_url=document.source_url,
                    current_version_id=document.current_version_id,
                    custom_metadata=document.custom_metadata,
                    updated_at=document.updated_at,
                    deleted_at=document.deleted_at,
                )
            ),
        )
        if result.rowcount == 0:
            raise LookupError("Document does not exist")

    async def replace_acl(
        self,
        document_id: UUID,
        entries: Sequence[AccessControlEntry],
    ) -> None:
        await self._session.execute(
            delete(DocumentAclModel).where(DocumentAclModel.document_id == document_id)
        )
        for item in entries:
            self._session.add(
                DocumentAclModel(
                    document_id=document_id,
                    principal_type=item.principal_type.value,
                    principal_id=item.principal_id,
                    effect=item.effect.value,
                )
            )

    async def get_version(self, version_id: UUID) -> DocumentVersion | None:
        row = await self._session.get(DocumentVersionModel, version_id)
        return _version_entity(row) if row else None

    async def get_versions(self, version_ids: Sequence[UUID]) -> list[DocumentVersion]:
        unique_ids = tuple(dict.fromkeys(version_ids))
        if not unique_ids:
            return []
        rows = (
            await self._session.scalars(
                select(DocumentVersionModel).where(DocumentVersionModel.id.in_(unique_ids))
            )
        ).all()
        return [_version_entity(row) for row in rows]

    async def find_version_by_hash(
        self,
        document_id: UUID,
        content_hash: str,
        embedding_model: str,
        embedding_model_revision: str,
    ) -> DocumentVersion | None:
        row = await self._session.scalar(
            select(DocumentVersionModel).where(
                DocumentVersionModel.document_id == document_id,
                DocumentVersionModel.content_hash == content_hash,
                DocumentVersionModel.embedding_model == embedding_model,
                DocumentVersionModel.embedding_model_revision == embedding_model_revision,
            )
        )
        return _version_entity(row) if row else None

    async def add_version(self, version: DocumentVersion) -> None:
        self._session.add(_version_model(version))
        # The persistence models intentionally do not expose ORM relationships.
        # Flush the parent row before dependent chunks/jobs are staged so that
        # SQLAlchemy does not emit child INSERTs before this foreign-key target.
        await self._session.flush()

    async def update_version_metadata(self, version: DocumentVersion) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(DocumentVersionModel)
                .where(DocumentVersionModel.id == version.id)
                .values(
                    version_number=version.version_number,
                    source_version=version.source_version,
                    effective_at=version.effective_at,
                    expires_at=version.expires_at,
                    review_at=version.review_at,
                )
            ),
        )
        if result.rowcount == 0:
            raise LookupError("Document version does not exist")

    async def update_version_status(
        self,
        version_id: UUID,
        status: IndexingStatus,
        indexed_at: datetime | None = None,
    ) -> None:
        await self._session.execute(
            update(DocumentVersionModel)
            .where(DocumentVersionModel.id == version_id)
            .values(indexing_status=status.value, indexed_at=indexed_at)
        )

    async def set_current_version(self, document_id: UUID, version_id: UUID) -> None:
        await self._session.execute(
            update(DocumentModel)
            .where(DocumentModel.id == document_id)
            .values(current_version_id=version_id, updated_at=datetime.now().astimezone())
        )

    async def add_chunks(self, chunks: Sequence[Chunk]) -> None:
        self._session.add_all([_chunk_model(chunk) for chunk in chunks])

    async def list_chunks(self, version_id: UUID) -> list[Chunk]:
        rows = (
            await self._session.scalars(
                select(ChunkModel)
                .where(ChunkModel.version_id == version_id)
                .order_by(ChunkModel.chunk_index)
            )
        ).all()
        return [_chunk_entity(row) for row in rows]

    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]:
        if not chunk_ids:
            return []
        rows = (
            await self._session.scalars(select(ChunkModel).where(ChunkModel.id.in_(chunk_ids)))
        ).all()
        return [_chunk_entity(row) for row in rows]

    async def update_chunks_status(
        self,
        version_id: UUID,
        status: IndexingStatus,
        point_ids: dict[UUID, str] | None = None,
    ) -> None:
        if point_ids is None:
            await self._session.execute(
                update(ChunkModel)
                .where(ChunkModel.version_id == version_id)
                .values(indexing_status=status.value)
            )
            return
        for chunk_id, point_id in point_ids.items():
            await self._session.execute(
                update(ChunkModel)
                .where(ChunkModel.id == chunk_id, ChunkModel.version_id == version_id)
                .values(indexing_status=status.value, qdrant_point_id=point_id)
            )

    async def _to_entity(self, row: DocumentModel) -> Document:
        acl_rows = (
            await self._session.scalars(
                select(DocumentAclModel).where(DocumentAclModel.document_id == row.id)
            )
        ).all()
        return _document_entity(row, acl_rows)


class SqlAlchemyJobRepository:
    """IndexingJob 的查詢、狀態轉移與 attempt persistence。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: UUID) -> IndexingJob | None:
        row = await self._session.get(IndexingJobModel, job_id)
        return _job_entity(row) if row else None

    async def add(self, job: IndexingJob) -> None:
        self._session.add(_job_model(job))
        # Outbox events reference indexing_jobs. Without an ORM relationship,
        # an explicit flush is required to preserve the aggregate write order.
        await self._session.flush()

    async def find_active_for_version(
        self,
        document_id: UUID,
        version_id: UUID | None,
    ) -> IndexingJob | None:
        query = select(IndexingJobModel).where(
            IndexingJobModel.document_id == document_id,
            IndexingJobModel.job_type == JobType.INDEX_VERSION.value,
            IndexingJobModel.status.in_([JobStatus.PENDING.value, JobStatus.PROCESSING.value]),
        )
        if version_id is None:
            query = query.where(IndexingJobModel.version_id.is_(None))
        else:
            query = query.where(IndexingJobModel.version_id == version_id)
        row = await self._session.scalar(query.order_by(IndexingJobModel.created_at.desc()))
        return _job_entity(row) if row else None

    async def find_active_for_document(
        self,
        document_id: UUID,
        job_type: JobType,
    ) -> IndexingJob | None:
        row = await self._session.scalar(
            select(IndexingJobModel)
            .where(
                IndexingJobModel.document_id == document_id,
                IndexingJobModel.job_type == job_type.value,
                IndexingJobModel.status.in_([JobStatus.PENDING.value, JobStatus.PROCESSING.value]),
            )
            .order_by(IndexingJobModel.created_at.desc(), IndexingJobModel.id.desc())
        )
        return _job_entity(row) if row else None

    async def find_latest_for_document(
        self,
        document_id: UUID,
        job_type: JobType,
    ) -> IndexingJob | None:
        row = await self._session.scalar(
            select(IndexingJobModel)
            .where(
                IndexingJobModel.document_id == document_id,
                IndexingJobModel.job_type == job_type.value,
            )
            .order_by(IndexingJobModel.created_at.desc(), IndexingJobModel.id.desc())
        )
        return _job_entity(row) if row else None

    async def mark_processing(self, job_id: UUID, started_at: datetime) -> None:
        await self._session.execute(
            update(IndexingJobModel)
            .where(IndexingJobModel.id == job_id)
            .values(status=JobStatus.PROCESSING.value, started_at=started_at)
        )

    async def mark_completed(self, job_id: UUID, completed_at: datetime) -> None:
        await self._session.execute(
            update(IndexingJobModel)
            .where(IndexingJobModel.id == job_id)
            .values(
                status=JobStatus.COMPLETED.value,
                completed_at=completed_at,
                error_code=None,
                error_message=None,
            )
        )

    async def mark_failed(
        self,
        job_id: UUID,
        error_code: str,
        error_message: str,
        completed_at: datetime,
    ) -> None:
        await self._session.execute(
            update(IndexingJobModel)
            .where(IndexingJobModel.id == job_id)
            .values(
                status=JobStatus.FAILED.value,
                error_code=error_code[:255],
                error_message=error_message,
                completed_at=completed_at,
            )
        )

    async def increment_attempt(self, job_id: UUID) -> int:
        row = await self._session.scalar(
            update(IndexingJobModel)
            .where(IndexingJobModel.id == job_id)
            .values(attempt_count=IndexingJobModel.attempt_count + 1)
            .returning(IndexingJobModel.attempt_count)
        )
        if row is None:
            raise LookupError("Job does not exist")
        return row


class SqlAlchemyOutboxRepository:
    """使用 row locking 實作可由多 worker 安全競爭的 durable queue。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: OutboxEvent) -> None:
        self._session.add(_outbox_model(event))

    async def claim_next(
        self,
        now: datetime,
        lease_expired_before: datetime,
    ) -> OutboxEvent | None:
        row = await self._session.scalar(
            select(OutboxEventModel)
            .where(
                OutboxEventModel.available_at <= now,
                or_(
                    OutboxEventModel.status == OutboxStatus.PENDING.value,
                    (
                        (OutboxEventModel.status == OutboxStatus.PROCESSING.value)
                        & (OutboxEventModel.locked_at < lease_expired_before)
                    ),
                ),
            )
            .order_by(OutboxEventModel.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.status = OutboxStatus.PROCESSING.value
        row.locked_at = now
        row.attempt_count += 1
        await self._session.flush()
        return _outbox_entity(row)

    async def mark_processed(self, event_id: UUID, processed_at: datetime) -> None:
        await self._session.execute(
            update(OutboxEventModel)
            .where(OutboxEventModel.id == event_id)
            .values(
                status=OutboxStatus.PROCESSED.value,
                processed_at=processed_at,
                locked_at=None,
                last_error=None,
            )
        )

    async def reschedule(
        self,
        event_id: UUID,
        available_at: datetime,
        error_message: str,
    ) -> None:
        await self._session.execute(
            update(OutboxEventModel)
            .where(OutboxEventModel.id == event_id)
            .values(
                status=OutboxStatus.PENDING.value,
                available_at=available_at,
                locked_at=None,
                last_error=error_message,
            )
        )

    async def mark_failed(self, event_id: UUID, error_message: str) -> None:
        await self._session.execute(
            update(OutboxEventModel)
            .where(OutboxEventModel.id == event_id)
            .values(
                status=OutboxStatus.FAILED.value,
                locked_at=None,
                last_error=error_message,
            )
        )


def _document_entity(
    row: DocumentModel,
    acl_rows: Sequence[DocumentAclModel],
) -> Document:
    """Mapper：ORM row/ACL rows -> framework-free Domain Document。"""

    return Document(
        id=row.id,
        source_type=row.source_type,
        source_item_id=row.source_item_id,
        title=row.title,
        document_type=row.document_type,
        owner_department=row.owner_department,
        document_owner=row.document_owner,
        confidentiality=Confidentiality(row.confidentiality),
        status=DocumentStatus(row.status),
        language=row.language,
        external_llm_allowed=row.external_llm_allowed,
        source_path=row.source_path,
        source_url=row.source_url,
        current_version_id=row.current_version_id,
        custom_metadata=row.custom_metadata,
        acl=[
            AccessControlEntry(
                principal_type=PrincipalType(acl.principal_type),
                principal_id=acl.principal_id,
                effect=PermissionEffect(acl.effect),
            )
            for acl in acl_rows
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _document_model(document: Document) -> DocumentModel:
    """Mapper：Domain Document -> 新增用 ORM model。"""

    return DocumentModel(
        id=document.id,
        source_type=document.source_type,
        source_item_id=document.source_item_id,
        title=document.title,
        document_type=document.document_type,
        owner_department=document.owner_department,
        document_owner=document.document_owner,
        confidentiality=document.confidentiality.value,
        status=document.status.value,
        language=document.language,
        external_llm_allowed=document.external_llm_allowed,
        source_path=document.source_path,
        source_url=document.source_url,
        current_version_id=document.current_version_id,
        custom_metadata=document.custom_metadata,
        created_at=document.created_at,
        updated_at=document.updated_at,
        deleted_at=document.deleted_at,
    )


def _version_model(version: DocumentVersion) -> DocumentVersionModel:
    return DocumentVersionModel(
        id=version.id,
        document_id=version.document_id,
        version_number=version.version_number,
        source_version=version.source_version,
        content_hash=version.content_hash,
        mime_type=version.mime_type,
        file_size=version.file_size,
        effective_at=version.effective_at,
        expires_at=version.expires_at,
        review_at=version.review_at,
        parser_name=version.parser_name,
        parser_version=version.parser_version,
        embedding_model=version.embedding_model,
        embedding_model_revision=version.embedding_model_revision,
        embedding_dimension=version.embedding_dimension,
        chunk_count=version.chunk_count,
        indexing_status=version.indexing_status.value,
        indexed_at=version.indexed_at,
        created_at=version.created_at,
    )


def _version_entity(row: DocumentVersionModel) -> DocumentVersion:
    return DocumentVersion(
        id=row.id,
        document_id=row.document_id,
        version_number=row.version_number,
        source_version=row.source_version,
        content_hash=row.content_hash,
        mime_type=row.mime_type,
        file_size=row.file_size,
        effective_at=row.effective_at,
        expires_at=row.expires_at,
        review_at=row.review_at,
        parser_name=row.parser_name,
        parser_version=row.parser_version,
        embedding_model=row.embedding_model,
        embedding_model_revision=row.embedding_model_revision,
        embedding_dimension=row.embedding_dimension,
        chunk_count=row.chunk_count,
        indexing_status=IndexingStatus(row.indexing_status),
        indexed_at=row.indexed_at,
        created_at=row.created_at,
    )


def _chunk_model(chunk: Chunk) -> ChunkModel:
    return ChunkModel(
        id=chunk.id,
        document_id=chunk.document_id,
        version_id=chunk.version_id,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        content_hash=chunk.content_hash,
        section_path=chunk.section_path,
        page_number=chunk.page_number,
        token_count=chunk.token_count,
        qdrant_point_id=chunk.qdrant_point_id,
        indexing_status=chunk.indexing_status.value,
        created_at=chunk.created_at,
    )


def _chunk_entity(row: ChunkModel) -> Chunk:
    return Chunk(
        id=row.id,
        document_id=row.document_id,
        version_id=row.version_id,
        chunk_index=row.chunk_index,
        content=row.content,
        content_hash=row.content_hash,
        section_path=row.section_path,
        page_number=row.page_number,
        token_count=row.token_count,
        qdrant_point_id=row.qdrant_point_id,
        indexing_status=IndexingStatus(row.indexing_status),
        created_at=row.created_at,
    )


def _job_model(job: IndexingJob) -> IndexingJobModel:
    return IndexingJobModel(
        id=job.id,
        job_type=job.job_type.value,
        document_id=job.document_id,
        version_id=job.version_id,
        status=job.status.value,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        error_code=job.error_code,
        error_message=job.error_message,
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
    )


def _job_entity(row: IndexingJobModel) -> IndexingJob:
    return IndexingJob(
        id=row.id,
        job_type=JobType(row.job_type),
        document_id=row.document_id,
        version_id=row.version_id,
        status=JobStatus(row.status),
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        error_code=row.error_code,
        error_message=row.error_message,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
    )


def _outbox_model(event: OutboxEvent) -> OutboxEventModel:
    return OutboxEventModel(
        id=event.id,
        event_type=event.event_type.value,
        aggregate_id=event.aggregate_id,
        job_id=event.job_id,
        payload=event.payload,
        status=event.status.value,
        attempt_count=event.attempt_count,
        available_at=event.available_at,
        locked_at=event.locked_at,
        processed_at=event.processed_at,
        last_error=event.last_error,
        created_at=event.created_at,
    )


def _outbox_entity(row: OutboxEventModel) -> OutboxEvent:
    return OutboxEvent(
        id=row.id,
        event_type=JobType(row.event_type),
        aggregate_id=row.aggregate_id,
        job_id=row.job_id,
        payload=row.payload,
        status=OutboxStatus(row.status),
        attempt_count=row.attempt_count,
        available_at=row.available_at,
        locked_at=row.locked_at,
        processed_at=row.processed_at,
        last_error=row.last_error,
        created_at=row.created_at,
    )

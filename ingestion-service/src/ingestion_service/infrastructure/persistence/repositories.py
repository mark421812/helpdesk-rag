"""以 SQLAlchemy 實作 Ingestion repository ports 與 ORM/Domain mapping。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import exists, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ingestion_service.domain.entities import IngestionJob, Source, SourceItem, SyncRun
from ingestion_service.domain.enums import (
    IngestionJobStatus,
    IngestionJobType,
    PendingOperation,
    SourceItemStatus,
    SourceType,
    SyncRunStatus,
    SyncTrigger,
)
from ingestion_service.infrastructure.persistence.models import (
    IngestionJobModel,
    SourceItemModel,
    SourceModel,
    SyncRunModel,
)


class SqlAlchemySourceRepository:
    """SourceRepository port 的 SQLAlchemy adapter。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, source_id: UUID) -> Source | None:
        row = await self._session.get(SourceModel, source_id)
        return _source_entity(row) if row else None

    async def add(self, source: Source) -> None:
        self._session.add(_source_model(source))
        await self._session.flush()

    async def update(self, source: Source) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(SourceModel)
                .where(SourceModel.id == source.id)
                .values(
                    name=source.name,
                    source_type=source.source_type.value,
                    root_path=source.root_path,
                    enabled=source.enabled,
                    allowed_extensions=list(source.allowed_extensions),
                    max_file_size_bytes=source.max_file_size_bytes,
                    schedule_cron=source.schedule_cron,
                    timezone=source.timezone,
                    last_successful_scan_at=source.last_successful_scan_at,
                    updated_at=source.updated_at,
                )
            ),
        )
        if result.rowcount == 0:
            raise LookupError("Source does not exist")

    async def list_enabled(self, source_type: SourceType | None = None) -> Sequence[Source]:
        query = select(SourceModel).where(SourceModel.enabled.is_(True))
        if source_type is not None:
            query = query.where(SourceModel.source_type == source_type.value)
        rows = (await self._session.scalars(query.order_by(SourceModel.name))).all()
        return [_source_entity(row) for row in rows]


class SqlAlchemySourceItemRepository:
    """SourceItemRepository adapter；負責 ORM model 與 Domain entity 雙向映射。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, item_id: UUID) -> SourceItem | None:
        row = await self._session.get(SourceItemModel, item_id)
        return _item_entity(row) if row else None

    async def find_by_source_item_id(
        self,
        source_id: UUID,
        source_item_id: str,
    ) -> SourceItem | None:
        row = await self._session.scalar(
            select(SourceItemModel).where(
                SourceItemModel.source_id == source_id,
                SourceItemModel.source_item_hash == _source_item_hash(source_item_id),
                SourceItemModel.source_item_id == source_item_id,
            )
        )
        return _item_entity(row) if row else None

    async def add(self, item: SourceItem) -> None:
        self._session.add(_item_model(item))
        await self._session.flush()

    async def update(self, item: SourceItem) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(SourceItemModel)
                .where(SourceItemModel.id == item.id)
                .values(**_item_values(item))
            ),
        )
        if result.rowcount == 0:
            raise LookupError("Source item does not exist")

    async def list_for_source(self, source_id: UUID) -> Sequence[SourceItem]:
        rows = (
            await self._session.scalars(
                select(SourceItemModel)
                .where(SourceItemModel.source_id == source_id)
                .order_by(SourceItemModel.source_item_id)
            )
        ).all()
        return [_item_entity(row) for row in rows]

    async def list_items(
        self,
        *,
        source_id: UUID | None = None,
        status: SourceItemStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Sequence[SourceItem]:
        query = select(SourceItemModel)
        if source_id is not None:
            query = query.where(SourceItemModel.source_id == source_id)
        if status is not None:
            query = query.where(SourceItemModel.status == status.value)
        rows = (
            await self._session.scalars(
                query.order_by(SourceItemModel.updated_at.desc()).offset(offset).limit(limit)
            )
        ).all()
        return [_item_entity(row) for row in rows]


class SqlAlchemySyncRunRepository:
    """SyncRunRepository port 的 SQLAlchemy adapter。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, run_id: UUID) -> SyncRun | None:
        row = await self._session.get(SyncRunModel, run_id)
        return _run_entity(row) if row else None

    async def add(self, run: SyncRun) -> None:
        self._session.add(_run_model(run))
        await self._session.flush()

    async def update(self, run: SyncRun) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(SyncRunModel)
                .where(SyncRunModel.id == run.id)
                .values(
                    status=run.status.value,
                    discovered_count=run.discovered_count,
                    queued_count=run.queued_count,
                    missing_count=run.missing_count,
                    delete_queued_count=run.delete_queued_count,
                    error_message=run.error_message,
                    completed_at=run.completed_at,
                )
            ),
        )
        if result.rowcount == 0:
            raise LookupError("Sync run does not exist")


class SqlAlchemyIngestionJobRepository:
    """IngestionJobRepository adapter，包含多 worker 安全的 job claim。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: UUID) -> IngestionJob | None:
        row = await self._session.get(IngestionJobModel, job_id)
        return _job_entity(row) if row else None

    async def get_by_idempotency_key(self, idempotency_key: str) -> IngestionJob | None:
        row = await self._session.scalar(
            select(IngestionJobModel).where(IngestionJobModel.idempotency_key == idempotency_key)
        )
        return _job_entity(row) if row else None

    async def add(self, job: IngestionJob) -> None:
        self._session.add(_job_model(job))
        await self._session.flush()

    async def update(self, job: IngestionJob) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(IngestionJobModel)
                .where(IngestionJobModel.id == job.id)
                .values(**_job_values(job))
            ),
        )
        if result.rowcount == 0:
            raise LookupError("Ingestion job does not exist")

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> IngestionJob | None:
        other_job = aliased(IngestionJobModel)
        another_active_job_for_item = exists(
            select(other_job.id).where(
                other_job.item_id == IngestionJobModel.item_id,
                other_job.id != IngestionJobModel.id,
                other_job.status == IngestionJobStatus.PROCESSING.value,
                other_job.lease_expires_at > now,
            )
        )
        # FOR UPDATE SKIP LOCKED 讓多個 worker 跳過已鎖 row，不會同時 claim 同一 job。
        row = await self._session.scalar(
            select(IngestionJobModel)
            .join(SourceItemModel, SourceItemModel.id == IngestionJobModel.item_id)
            .where(
                IngestionJobModel.available_at <= now,
                ~another_active_job_for_item,
                or_(
                    IngestionJobModel.status == IngestionJobStatus.PENDING.value,
                    (
                        (IngestionJobModel.status == IngestionJobStatus.PROCESSING.value)
                        & (IngestionJobModel.lease_expires_at < now)
                    ),
                ),
            )
            .order_by(IngestionJobModel.available_at, IngestionJobModel.created_at)
            .with_for_update(
                of=(IngestionJobModel, SourceItemModel),
                skip_locked=True,
            )
            .limit(1)
        )
        if row is None:
            return None
        job = _job_entity(row)
        job.claim(worker_id, now, lease_for)
        _copy_job_values(row, job)
        await self._session.flush()
        return job


def _source_model(source: Source) -> SourceModel:
    """將 Domain Source 映射成新的 ORM row。"""

    return SourceModel(
        id=source.id,
        name=source.name,
        source_type=source.source_type.value,
        root_path=source.root_path,
        enabled=source.enabled,
        allowed_extensions=list(source.allowed_extensions),
        max_file_size_bytes=source.max_file_size_bytes,
        schedule_cron=source.schedule_cron,
        timezone=source.timezone,
        last_successful_scan_at=source.last_successful_scan_at,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _source_entity(row: SourceModel) -> Source:
    """將 ORM row 重建為含商業方法的 Domain Source。"""

    return Source(
        id=row.id,
        name=row.name,
        source_type=SourceType(row.source_type),
        root_path=row.root_path,
        enabled=row.enabled,
        allowed_extensions=tuple(row.allowed_extensions),
        max_file_size_bytes=row.max_file_size_bytes,
        schedule_cron=row.schedule_cron,
        timezone=row.timezone,
        last_successful_scan_at=row.last_successful_scan_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _item_model(item: SourceItem) -> SourceItemModel:
    return SourceItemModel(id=item.id, source_id=item.source_id, **_item_values(item))


def _item_values(item: SourceItem) -> dict[str, object]:
    return {
        "source_item_id": item.source_item_id,
        "source_item_hash": _source_item_hash(item.source_item_id),
        "source_path": item.source_path,
        "absolute_path": item.absolute_path,
        "source_version": item.source_version,
        "metadata_source_hash": item.metadata_source_hash,
        "mime_type": item.mime_type,
        "file_size": item.file_size,
        "modified_at": item.modified_at,
        "lifecycle_generation": item.lifecycle_generation,
        "status": item.status.value,
        "missing_scan_count": item.missing_scan_count,
        "last_seen_run_id": item.last_seen_run_id,
        "last_seen_at": item.last_seen_at,
        "last_successful_content_hash": item.last_successful_content_hash,
        "last_successful_metadata_hash": item.last_successful_metadata_hash,
        "pending_content_hash": item.pending_content_hash,
        "pending_metadata_hash": item.pending_metadata_hash,
        "pending_operation": item.pending_operation.value if item.pending_operation else None,
        "restore_requested": item.restore_requested,
        "knowledge_index_document_id": item.knowledge_index_document_id,
        "knowledge_index_version_id": item.knowledge_index_version_id,
        "knowledge_index_job_id": item.knowledge_index_job_id,
        "error_code": item.error_code,
        "error_message": item.error_message,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "completed_at": item.completed_at,
        "deleted_at": item.deleted_at,
    }


def _item_entity(row: SourceItemModel) -> SourceItem:
    return SourceItem(
        id=row.id,
        source_id=row.source_id,
        source_item_id=row.source_item_id,
        source_path=row.source_path,
        absolute_path=row.absolute_path,
        source_version=row.source_version,
        metadata_source_hash=row.metadata_source_hash,
        mime_type=row.mime_type,
        file_size=row.file_size,
        modified_at=row.modified_at,
        lifecycle_generation=row.lifecycle_generation,
        status=SourceItemStatus(row.status),
        missing_scan_count=row.missing_scan_count,
        last_seen_run_id=row.last_seen_run_id,
        last_seen_at=row.last_seen_at,
        last_successful_content_hash=row.last_successful_content_hash,
        last_successful_metadata_hash=row.last_successful_metadata_hash,
        pending_content_hash=row.pending_content_hash,
        pending_metadata_hash=row.pending_metadata_hash,
        pending_operation=(
            PendingOperation(row.pending_operation) if row.pending_operation else None
        ),
        restore_requested=row.restore_requested,
        knowledge_index_document_id=row.knowledge_index_document_id,
        knowledge_index_version_id=row.knowledge_index_version_id,
        knowledge_index_job_id=row.knowledge_index_job_id,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        deleted_at=row.deleted_at,
    )


def _run_model(run: SyncRun) -> SyncRunModel:
    return SyncRunModel(
        id=run.id,
        source_id=run.source_id,
        trigger=run.trigger.value,
        status=run.status.value,
        discovered_count=run.discovered_count,
        queued_count=run.queued_count,
        missing_count=run.missing_count,
        delete_queued_count=run.delete_queued_count,
        error_message=run.error_message,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.started_at,
    )


def _run_entity(row: SyncRunModel) -> SyncRun:
    return SyncRun(
        id=row.id,
        source_id=row.source_id,
        trigger=SyncTrigger(row.trigger),
        status=SyncRunStatus(row.status),
        discovered_count=row.discovered_count,
        queued_count=row.queued_count,
        missing_count=row.missing_count,
        delete_queued_count=row.delete_queued_count,
        error_message=row.error_message,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _job_model(job: IngestionJob) -> IngestionJobModel:
    return IngestionJobModel(
        id=job.id,
        source_id=job.source_id,
        item_id=job.item_id,
        sync_run_id=job.sync_run_id,
        job_type=job.job_type.value,
        idempotency_key=job.idempotency_key,
        **_job_values(job),
    )


def _job_values(job: IngestionJob) -> dict[str, object]:
    return {
        "status": job.status.value,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "available_at": job.available_at,
        "leased_by": job.leased_by,
        "lease_expires_at": job.lease_expires_at,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
    }


def _copy_job_values(row: IngestionJobModel, job: IngestionJob) -> None:
    for key, value in _job_values(job).items():
        setattr(row, key, value)


def _job_entity(row: IngestionJobModel) -> IngestionJob:
    return IngestionJob(
        id=row.id,
        source_id=row.source_id,
        item_id=row.item_id,
        sync_run_id=row.sync_run_id,
        job_type=IngestionJobType(row.job_type),
        idempotency_key=row.idempotency_key,
        status=IngestionJobStatus(row.status),
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        available_at=row.available_at,
        leased_by=row.leased_by,
        lease_expires_at=row.lease_expires_at,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _source_item_hash(source_item_id: str) -> str:
    return hashlib.sha256(source_item_id.encode("utf-8")).hexdigest()

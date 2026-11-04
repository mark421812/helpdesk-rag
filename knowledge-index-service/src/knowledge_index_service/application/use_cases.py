"""Knowledge Index Service 的 Application use cases。

本模組編排文件提交、embedding、搜尋、metadata 更新、重建索引、soft delete 與 durable
outbox worker。它只依賴 Domain 和 ports，不知道 SQLAlchemy/Qdrant/http 的具體實作。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from knowledge_index_service.application.dtos import (
    DocumentSubmission,
    EmbeddingResult,
    JobView,
    MetadataPatch,
    SearchRequest,
    SearchResult,
    SearchResultItem,
    SubmitDocumentResult,
    UnsetValue,
)
from knowledge_index_service.application.ports import (
    EmbeddingModelPort,
    UnitOfWorkFactory,
    VectorStorePort,
)
from knowledge_index_service.domain.entities import (
    Chunk,
    Document,
    DocumentVersion,
    IndexingJob,
    OutboxEvent,
    SearchFilters,
    VectorPoint,
    VectorSearchHit,
    utc_now,
)
from knowledge_index_service.domain.enums import (
    DocumentStatus,
    EmbeddingInputType,
    IndexingStatus,
    JobType,
)
from knowledge_index_service.domain.exceptions import (
    CapacityExceededError,
    ConflictError,
    EntityNotFoundError,
    ValidationError,
)


class SubmitDocument:
    """建立新文件／版本，或以冪等方式重用、恢復既有版本。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        embedding_model: EmbeddingModelPort,
        max_chunks: int = 10_000,
        max_characters: int = 20_000_000,
    ) -> None:
        self._uow_factory = uow_factory
        self._embedding_model = embedding_model
        self._max_chunks = max_chunks
        self._max_characters = max_characters

    async def execute(self, submission: DocumentSubmission) -> SubmitDocumentResult:
        """驗證容量與 fingerprint，並在單一 transaction 排入 index outbox。"""

        # 先做不需要 I/O 的廉價驗證，避免無效 request 佔用 DB transaction。
        if not submission.chunks:
            raise ValidationError("At least one chunk is required")
        if len(submission.chunks) > self._max_chunks:
            raise CapacityExceededError(f"A document may contain at most {self._max_chunks} chunks")
        total_characters = sum(len(chunk.content) for chunk in submission.chunks)
        if total_characters > self._max_characters:
            raise CapacityExceededError(
                f"Document chunk content is limited to {self._max_characters} characters"
            )
        if len(submission.content_hash) != 64:
            raise ValidationError("content_hash must be a SHA-256 hexadecimal value")
        try:
            int(submission.content_hash, 16)
        except ValueError as exc:
            raise ValidationError("content_hash must be a SHA-256 hexadecimal value") from exc
        model = await self._embedding_model.model_info()

        # 每個 ``async with`` 都建立獨立 DB session/transaction。
        async with self._uow_factory() as uow:
            document = await uow.documents.find_by_source(
                submission.source_type,
                submission.source_item_id,
            )
            restoring = document is not None and document.deleted_at is not None
            version_id = uuid4()
            if document is None:
                document = Document(
                    id=uuid4(),
                    source_type=submission.source_type,
                    source_item_id=submission.source_item_id,
                    title=submission.title,
                    document_type=submission.document_type,
                    owner_department=submission.owner_department,
                    document_owner=submission.document_owner,
                    confidentiality=submission.confidentiality,
                    status=submission.status,
                    language=submission.language,
                    external_llm_allowed=submission.external_llm_allowed,
                    source_path=submission.source_path,
                    source_url=submission.source_url,
                    custom_metadata=dict(submission.custom_metadata),
                    acl=list(submission.acl),
                )
                await uow.documents.add(document)
            else:
                existing_version = await uow.documents.find_version_by_hash(
                    document.id,
                    submission.content_hash,
                    model.model_id,
                    model.revision,
                )
                if existing_version is not None:
                    active_job = await uow.jobs.find_active_for_version(
                        document.id,
                        existing_version.id,
                    )
                    latest_active_index_job = await uow.jobs.find_active_for_document(
                        document.id,
                        JobType.INDEX_VERSION,
                    )
                    should_activate = (
                        restoring
                        or document.current_version_id != existing_version.id
                        or (
                            latest_active_index_job is not None
                            and latest_active_index_job.version_id != existing_version.id
                        )
                    )
                    if should_activate:
                        document = _activate_submission_metadata(
                            document,
                            submission,
                            existing_version.id,
                        )
                        existing_version = _apply_submission_version_metadata(
                            existing_version,
                            submission,
                        )
                        await uow.documents.update(document)
                        await uow.documents.replace_acl(document.id, document.acl)
                        await uow.documents.update_version_metadata(existing_version)
                        activation_job = None if restoring else active_job
                        result_status = existing_version.indexing_status
                        if activation_job is None:
                            activation_job, event = _new_index_job(
                                document.id,
                                existing_version.id,
                            )
                            await uow.documents.update_version_status(
                                existing_version.id,
                                IndexingStatus.PENDING,
                            )
                            await uow.jobs.add(activation_job)
                            await uow.outbox.add(event)
                            result_status = IndexingStatus.PENDING
                        await uow.commit()
                        return SubmitDocumentResult(
                            document_id=document.id,
                            version_id=existing_version.id,
                            job_id=activation_job.id,
                            indexing_status=result_status,
                            created=False,
                        )
                    result_status = existing_version.indexing_status
                    if (
                        active_job is None
                        and existing_version.indexing_status != IndexingStatus.INDEXED
                    ):
                        active_job, event = _new_index_job(document.id, existing_version.id)
                        await uow.documents.update_version_status(
                            existing_version.id,
                            IndexingStatus.PENDING,
                        )
                        await uow.jobs.add(active_job)
                        await uow.outbox.add(event)
                        await uow.commit()
                        result_status = IndexingStatus.PENDING
                    return SubmitDocumentResult(
                        document_id=document.id,
                        version_id=existing_version.id,
                        job_id=active_job.id if active_job else None,
                        indexing_status=result_status,
                        created=False,
                    )
                document = _apply_submission_metadata(document, submission)
                if restoring:
                    document = replace(
                        document,
                        current_version_id=version_id,
                        deleted_at=None,
                        updated_at=utc_now(),
                    )
                await uow.documents.update(document)
                await uow.documents.replace_acl(document.id, document.acl)

            version = DocumentVersion(
                id=version_id,
                document_id=document.id,
                version_number=submission.version_number,
                source_version=submission.source_version,
                content_hash=submission.content_hash,
                mime_type=submission.mime_type,
                file_size=submission.file_size,
                effective_at=submission.effective_at,
                expires_at=submission.expires_at,
                review_at=submission.review_at,
                parser_name=submission.parser_name,
                parser_version=submission.parser_version,
                embedding_model=model.model_id,
                embedding_model_revision=model.revision,
                embedding_dimension=model.dimension,
                chunk_count=len(submission.chunks),
            )
            # enumerate 同時取得 0-based index 與 item；comprehension 建立整份 chunk list。
            chunks = [
                Chunk(
                    id=uuid4(),
                    document_id=document.id,
                    version_id=version_id,
                    chunk_index=index,
                    content=item.content,
                    content_hash=hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                    section_path=item.section_path,
                    page_number=item.page_number,
                    token_count=item.token_count,
                )
                for index, item in enumerate(submission.chunks)
            ]
            job, event = _new_index_job(document.id, version.id)
            await uow.documents.add_version(version)
            await uow.documents.add_chunks(chunks)
            await uow.jobs.add(job)
            await uow.outbox.add(event)
            await uow.commit()
            return SubmitDocumentResult(
                document_id=document.id,
                version_id=version.id,
                job_id=job.id,
                indexing_status=version.indexing_status,
                created=True,
            )


class EmbedTexts:
    """提供有容量保護的內部 raw embedding use case。"""

    def __init__(
        self,
        embedding_model: EmbeddingModelPort,
        max_texts: int,
        max_characters_per_text: int,
    ) -> None:
        self._embedding_model = embedding_model
        self._max_texts = max_texts
        self._max_characters_per_text = max_characters_per_text

    async def execute(
        self,
        texts: Sequence[str],
        input_type: EmbeddingInputType,
    ) -> EmbeddingResult:
        """驗證批次後呼叫 EmbeddingModelPort，不直接 import 模型套件。"""

        if not texts:
            raise ValidationError("At least one text is required")
        if len(texts) > self._max_texts:
            raise CapacityExceededError(f"At most {self._max_texts} texts are allowed")
        if any(not text.strip() for text in texts):
            raise ValidationError("Embedding inputs must not be blank")
        if any(len(text) > self._max_characters_per_text for text in texts):
            raise CapacityExceededError(
                f"Each text is limited to {self._max_characters_per_text} characters"
            )
        model = await self._embedding_model.model_info()
        vectors = await self._embedding_model.embed(texts, input_type)
        return EmbeddingResult(
            vectors=tuple(tuple(vector) for vector in vectors),
            model_id=model.model_id,
            model_revision=model.revision,
            dimension=model.dimension,
        )


class SearchKnowledge:
    """產生 query vector、取得 Qdrant 候選，再以 PostgreSQL canonical 狀態驗證。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        embedding_model: EmbeddingModelPort,
        vector_store: VectorStorePort,
        max_limit: int,
        max_candidates: int = 500,
        business_timezone: str = "Asia/Taipei",
    ) -> None:
        self._uow_factory = uow_factory
        self._embedding_model = embedding_model
        self._vector_store = vector_store
        self._max_limit = max_limit
        self._max_candidates = max(max_candidates, max_limit)
        self._business_timezone = ZoneInfo(business_timezone)

    async def execute(self, request: SearchRequest) -> SearchResult:
        """執行 progressive vector search，直到回滿或達候選上限。"""

        if not request.query.strip():
            raise ValidationError("Query must not be blank")
        if request.limit <= 0 or request.limit > self._max_limit:
            raise ValidationError(f"limit must be between 1 and {self._max_limit}")
        # conditional expression：若 statuses 是空 tuple，強制改回 published（fail closed）。
        filters = (
            request.filters
            if request.filters.statuses
            else replace(request.filters, statuses=(DocumentStatus.PUBLISHED,))
        )
        model = await self._embedding_model.model_info()
        await self._vector_store.ensure_collection(model.dimension)
        vector = (await self._embedding_model.embed([request.query], EmbeddingInputType.QUERY))[0]
        today = utc_now().astimezone(self._business_timezone).date()
        candidate_offset = 0
        page_size = min(request.limit * 3, self._max_candidates)
        seen_chunk_ids: set[UUID] = set()
        items: list[SearchResultItem] = []

        # while 適合不知道要取幾頁的 progressive fetch；每次 offset 往後移。
        while candidate_offset < self._max_candidates and len(items) < request.limit:
            fetch_limit = min(page_size, self._max_candidates - candidate_offset)
            hits = await self._vector_store.search(
                vector=vector,
                limit=fetch_limit,
                filters=filters,
                score_threshold=request.score_threshold,
                offset=candidate_offset,
            )
            if not hits:
                break
            candidate_offset += len(hits)
            page_items = await self._hydrate_search_hits(
                hits,
                filters,
                today,
                seen_chunk_ids,
            )
            items.extend(page_items[: request.limit - len(items)])
            if len(hits) < fetch_limit:
                break
            page_size = min(page_size * 2, self._max_candidates - candidate_offset)

        return SearchResult(
            items=tuple(items),
            model_id=model.model_id,
            model_revision=model.revision,
        )

    async def _hydrate_search_hits(
        self,
        hits: Sequence[VectorSearchHit],
        filters: SearchFilters,
        today: date,
        seen_chunk_ids: set[UUID],
    ) -> list[SearchResultItem]:
        """批次 hydrate candidate IDs，並以最新 DB ACL/lifecycle 做第二層檢查。"""

        hit_by_chunk_id: dict[UUID, VectorSearchHit] = {}
        ordered_ids: list[UUID] = []
        for hit in hits:
            raw_chunk_id = hit.payload.get("chunk_id")
            if not raw_chunk_id:
                continue
            try:
                chunk_id = UUID(str(raw_chunk_id))
            except ValueError:
                continue
            if chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk_id)
            hit_by_chunk_id[chunk_id] = hit
            ordered_ids.append(chunk_id)

        async with self._uow_factory() as uow:
            chunks = await uow.documents.get_chunks(ordered_ids)
            if not chunks:
                return []
            loaded_documents = await uow.documents.get_many(
                tuple({chunk.document_id for chunk in chunks})
            )
            loaded_versions = await uow.documents.get_versions(
                tuple({chunk.version_id for chunk in chunks})
            )

        # dict comprehension 把 list 轉成以 UUID 查找的 map，後續每次 lookup 平均 O(1)。
        documents = {document.id: document for document in loaded_documents}
        versions = {version.id: version for version in loaded_versions}

        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        items: list[SearchResultItem] = []
        for chunk_id in ordered_ids:
            chunk = chunks_by_id.get(chunk_id)
            document = documents.get(chunk.document_id) if chunk else None
            version = versions.get(chunk.version_id) if chunk else None
            matched_hit = hit_by_chunk_id.get(chunk_id)
            if chunk is None or document is None or version is None or matched_hit is None:
                continue
            if not _document_matches_filters(document, version, chunk, filters, today):
                continue
            items.append(
                SearchResultItem(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    version_id=chunk.version_id,
                    score=matched_hit.score,
                    content=chunk.content,
                    title=document.title,
                    document_type=document.document_type,
                    source_path=document.source_path,
                    source_url=document.source_url,
                    section_path=chunk.section_path,
                    page_number=chunk.page_number,
                    token_count=_search_token_count(chunk),
                    chunk_index=chunk.chunk_index,
                    version_number=version.version_number,
                    effective_at=version.effective_at,
                    expires_at=version.expires_at,
                    external_llm_allowed=document.external_llm_allowed,
                    confidentiality=document.confidentiality,
                    language=document.language,
                    status=document.status,
                    custom_metadata=document.custom_metadata,
                )
            )
        return items


class UpdateDocumentMetadata:
    """更新 canonical metadata/ACL，並以 outbox 非同步同步 Qdrant payload。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, document_id: UUID, patch: MetadataPatch) -> JobView:
        """同交易保存 metadata、sync job 與 event；不重新計算 embedding。"""

        async with self._uow_factory() as uow:
            document = await uow.documents.get(document_id)
            if document is None or document.deleted_at is not None:
                raise EntityNotFoundError("Document not found")
            document = _apply_patch(document, patch)
            version: DocumentVersion | None = None
            if _has_version_patch(patch):
                if document.current_version_id is None:
                    raise ConflictError("Document has no current version")
                current_version = await uow.documents.get_version(document.current_version_id)
                if current_version is None:
                    raise ConflictError("Current document version is missing")
                version = _apply_version_patch(current_version, patch)
            await uow.documents.update(document)
            if version is not None:
                await uow.documents.update_version_metadata(version)
            if patch.acl is not None:
                await uow.documents.replace_acl(document.id, document.acl)
            job = IndexingJob(
                id=uuid4(),
                job_type=JobType.SYNC_METADATA,
                document_id=document.id,
                version_id=document.current_version_id,
            )
            event = OutboxEvent(
                id=uuid4(),
                event_type=JobType.SYNC_METADATA,
                aggregate_id=document.id,
                job_id=job.id,
                payload={"document_id": str(document.id)},
            )
            await uow.jobs.add(job)
            await uow.outbox.add(event)
            await uow.commit()
            return _job_view(job)


class ReindexDocument:
    """為目前版本建立冪等 reindex job。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, document_id: UUID) -> JobView:
        async with self._uow_factory() as uow:
            document = await uow.documents.get(document_id)
            if document is None or document.deleted_at is not None:
                raise EntityNotFoundError("Document not found")
            if document.current_version_id is None:
                raise ConflictError("Document has no current version")
            version = await uow.documents.get_version(document.current_version_id)
            if version is None:
                raise ConflictError("Current document version is missing")
            active = await uow.jobs.find_active_for_version(document.id, version.id)
            if active:
                return _job_view(active)
            job, event = _new_index_job(document.id, version.id)
            await uow.documents.update_version_status(version.id, IndexingStatus.PENDING)
            await uow.jobs.add(job)
            await uow.outbox.add(event)
            await uow.commit()
            return _job_view(job)


class DeleteDocument:
    """先 soft-delete canonical document，再排入刪除 vectors 的 outbox。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, document_id: UUID) -> JobView:
        async with self._uow_factory() as uow:
            document = await uow.documents.get_for_update(document_id)
            if document is None:
                raise EntityNotFoundError("Document not found")
            if document.deleted_at is not None:
                latest_job = await uow.jobs.find_latest_for_document(
                    document.id,
                    JobType.DELETE_DOCUMENT,
                )
                if latest_job is not None:
                    return _job_view(latest_job)
            else:
                document.deleted_at = utc_now()
                document.updated_at = utc_now()
                document.status = DocumentStatus.DELETED
                await uow.documents.update(document)
            job = IndexingJob(
                id=uuid4(),
                job_type=JobType.DELETE_DOCUMENT,
                document_id=document.id,
                version_id=document.current_version_id,
            )
            event = OutboxEvent(
                id=uuid4(),
                event_type=JobType.DELETE_DOCUMENT,
                aggregate_id=document.id,
                job_id=job.id,
                payload={"document_id": str(document.id)},
            )
            await uow.jobs.add(job)
            await uow.outbox.add(event)
            await uow.commit()
            return _job_view(job)


class GetJob:
    """以 ID 取得索引工作狀態。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, job_id: UUID) -> JobView:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get(job_id)
            if job is None:
                raise EntityNotFoundError("Job not found")
            return _job_view(job)


class ProcessNextOutboxEvent:
    """Worker 每次 claim 並處理一筆 durable outbox event。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        embedding_model: EmbeddingModelPort,
        vector_store: VectorStorePort,
        lease_seconds: int,
        retry_base_seconds: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._embedding_model = embedding_model
        self._vector_store = vector_store
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds

    async def execute(self) -> bool:
        """有處理 event 回傳 True；沒有工作回傳 False，供 worker 決定是否等待。"""

        now = utc_now()
        async with self._uow_factory() as uow:
            event = await uow.outbox.claim_next(
                now=now,
                lease_expired_before=now - timedelta(seconds=self._lease_seconds),
            )
            if event is None:
                return False
            job = await uow.jobs.get(event.job_id)
            if job is None:
                await uow.outbox.mark_failed(event.id, "Associated job does not exist")
                await uow.commit()
                return True
            await uow.jobs.mark_processing(job.id, now)
            attempt = await uow.jobs.increment_attempt(job.id)
            if event.event_type == JobType.INDEX_VERSION and job.version_id:
                await uow.documents.update_version_status(
                    job.version_id,
                    IndexingStatus.PROCESSING,
                )
            await uow.commit()

        # 先 commit claim/attempt，再做耗時外部 I/O，避免 transaction 長時間鎖住資料列。
        try:
            if event.event_type == JobType.INDEX_VERSION:
                await self._index_version(event.aggregate_id, UUID(event.payload["version_id"]))
            elif event.event_type == JobType.SYNC_METADATA:
                await self._sync_metadata(event.aggregate_id)
            elif event.event_type == JobType.DELETE_DOCUMENT:
                await self._delete_document_if_still_deleted(event.aggregate_id)
            else:
                raise ValidationError(f"Unsupported event type: {event.event_type}")
        except Exception as exc:
            # 預期不可重試錯誤直接 failed；暫時性錯誤則以 exponential backoff 重排。
            error_message = _safe_error_message(exc)
            async with self._uow_factory() as uow:
                non_retryable = isinstance(
                    exc,
                    (
                        CapacityExceededError,
                        ConflictError,
                        EntityNotFoundError,
                        ValidationError,
                    ),
                )
                if non_retryable or attempt >= job.max_attempts:
                    await uow.jobs.mark_failed(
                        job.id,
                        type(exc).__name__,
                        error_message,
                        utc_now(),
                    )
                    await uow.outbox.mark_failed(event.id, error_message)
                    if event.event_type == JobType.INDEX_VERSION and job.version_id:
                        await uow.documents.update_version_status(
                            job.version_id,
                            IndexingStatus.FAILED,
                        )
                else:
                    delay = self._retry_base_seconds * (2 ** (attempt - 1))
                    await uow.outbox.reschedule(
                        event.id,
                        utc_now() + timedelta(seconds=delay),
                        error_message,
                    )
                await uow.commit()
            return True

        async with self._uow_factory() as uow:
            await uow.jobs.mark_completed(job.id, utc_now())
            await uow.outbox.mark_processed(event.id, utc_now())
            await uow.commit()
        return True

    async def _index_version(self, document_id: UUID, version_id: UUID) -> None:
        """embedding 全部 chunks、寫入 Qdrant，最後才原子切換 current version。"""

        async with self._uow_factory() as uow:
            document = await uow.documents.get(document_id)
            version = await uow.documents.get_version(version_id)
            chunks = await uow.documents.list_chunks(version_id)
            current_version = (
                await uow.documents.get_version(document.current_version_id)
                if document is not None
                and document.current_version_id is not None
                and document.current_version_id != version_id
                else None
            )
        if document is None or version is None:
            raise EntityNotFoundError("Document or version no longer exists")
        if document.deleted_at is not None:
            raise ConflictError("Document was deleted before its version could be indexed")
        if _is_superseded_by_pending_version(version_id, current_version):
            raise ConflictError("Document version was superseded by a restore")
        if not chunks:
            raise ConflictError("Document version contains no chunks")
        model = await self._embedding_model.model_info()
        if (
            version.embedding_model != model.model_id
            or version.embedding_model_revision != model.revision
            or version.embedding_dimension != model.dimension
        ):
            raise ConflictError(
                "Document version embedding model does not match the configured model"
            )
        await self._vector_store.ensure_collection(model.dimension)
        vectors = await self._embedding_model.embed(
            [chunk.content for chunk in chunks],
            EmbeddingInputType.DOCUMENT,
        )
        async with self._uow_factory() as uow:
            latest_document = await uow.documents.get(document_id)
            current_version = (
                await uow.documents.get_version(latest_document.current_version_id)
                if latest_document is not None
                and latest_document.current_version_id is not None
                and latest_document.current_version_id != version_id
                else None
            )
        if latest_document is None:
            raise EntityNotFoundError("Document no longer exists")
        if latest_document.deleted_at is not None:
            await self._delete_document_if_still_deleted(document_id)
            raise ConflictError("Document was deleted while its version was being indexed")
        if _is_superseded_by_pending_version(version_id, current_version):
            raise ConflictError("Document version was superseded by a restore")
        document = latest_document
        # strict=True 確保 chunks/vectors 長度不同時立即失敗，不會靜默少寫資料。
        points = [
            VectorPoint(
                point_id=str(chunk.id),
                vector=vector,
                payload=_vector_payload(document, chunk, is_current=True),
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        await self._vector_store.upsert(points)
        async with self._uow_factory() as uow:
            latest_document = await uow.documents.get(document_id)
            current_version = (
                await uow.documents.get_version(latest_document.current_version_id)
                if latest_document is not None
                and latest_document.current_version_id is not None
                and latest_document.current_version_id != version_id
                else None
            )
        if latest_document is None:
            raise EntityNotFoundError("Document no longer exists")
        if latest_document.deleted_at is not None:
            await self._delete_document_if_still_deleted(document_id)
            raise ConflictError("Document was deleted while its version was being indexed")
        if _is_superseded_by_pending_version(version_id, current_version):
            if current_version is None:
                raise RuntimeError("Superseding version was not loaded")
            await self._vector_store.deactivate_document_versions(
                document_id,
                current_version.id,
            )
            raise ConflictError("Document version was superseded by a restore")
        await self._vector_store.deactivate_document_versions(document.id, version.id)
        async with self._uow_factory() as uow:
            await uow.documents.update_chunks_status(
                version.id,
                IndexingStatus.INDEXED,
                {chunk.id: str(chunk.id) for chunk in chunks},
            )
            await uow.documents.update_version_status(
                version.id,
                IndexingStatus.INDEXED,
                utc_now(),
            )
            await uow.documents.set_current_version(document.id, version.id)
            await uow.commit()

    async def _sync_metadata(self, document_id: UUID) -> None:
        """將 PostgreSQL 最新治理欄位同步到既有 Qdrant points。"""

        async with self._uow_factory() as uow:
            document = await uow.documents.get(document_id)
        if document is None:
            raise EntityNotFoundError("Document no longer exists")
        await self._vector_store.sync_document_payload(document)

    async def _delete_document_if_still_deleted(self, document_id: UUID) -> None:
        """執行 Qdrant delete 前後都重新確認 canonical tombstone，處理 restore race。"""

        async with self._uow_factory() as uow:
            document = await uow.documents.get(document_id)
        if document is None:
            raise EntityNotFoundError("Document no longer exists")
        if document.deleted_at is None:
            return

        await self._vector_store.delete_document(document_id)

        # A restore can commit while a previously claimed delete is talking to
        # Qdrant. Queue a fresh index after that delete so the latest canonical
        # state wins even if the earlier restore job raced with vector removal.
        async with self._uow_factory() as uow:
            latest_document = await uow.documents.get(document_id)
            if (
                latest_document is not None
                and latest_document.deleted_at is None
                and latest_document.current_version_id is not None
            ):
                version = await uow.documents.get_version(latest_document.current_version_id)
                if version is None:
                    raise ConflictError("Restored document current version is missing")
                recovery_job, recovery_event = _new_index_job(document_id, version.id)
                await uow.documents.update_version_status(version.id, IndexingStatus.PENDING)
                await uow.jobs.add(recovery_job)
                await uow.outbox.add(recovery_event)
                await uow.commit()


def _is_superseded_by_pending_version(
    version_id: UUID,
    current_version: DocumentVersion | None,
) -> bool:
    """判斷本次索引是否已被另一個 pending/processing 版本取代。"""

    return (
        current_version is not None
        and current_version.id != version_id
        and current_version.indexing_status in {IndexingStatus.PENDING, IndexingStatus.PROCESSING}
    )


def _apply_submission_metadata(document: Document, submission: DocumentSubmission) -> Document:
    """以 ``dataclasses.replace`` 複製 Document 並套用提交的 metadata。"""

    return replace(
        document,
        title=submission.title,
        document_type=submission.document_type,
        owner_department=submission.owner_department,
        document_owner=submission.document_owner,
        confidentiality=submission.confidentiality,
        status=submission.status,
        language=submission.language,
        external_llm_allowed=submission.external_llm_allowed,
        source_path=submission.source_path,
        source_url=submission.source_url,
        custom_metadata=dict(submission.custom_metadata),
        acl=list(submission.acl),
        updated_at=utc_now(),
    )


def _activate_submission_metadata(
    document: Document,
    submission: DocumentSubmission,
    version_id: UUID,
) -> Document:
    restored = _apply_submission_metadata(document, submission)
    return replace(
        restored,
        current_version_id=version_id,
        deleted_at=None,
        updated_at=utc_now(),
    )


def _apply_submission_version_metadata(
    version: DocumentVersion,
    submission: DocumentSubmission,
) -> DocumentVersion:
    return replace(
        version,
        version_number=submission.version_number,
        source_version=submission.source_version,
        effective_at=submission.effective_at,
        expires_at=submission.expires_at,
        review_at=submission.review_at,
    )


def _apply_patch(document: Document, patch: MetadataPatch) -> Document:
    has_document_patch = any(
        value is not None
        for value in (
            patch.title,
            patch.document_type,
            patch.owner_department,
            patch.document_owner,
            patch.confidentiality,
            patch.status,
            patch.language,
            patch.external_llm_allowed,
            patch.source_path,
            patch.custom_metadata,
            patch.acl,
        )
    ) or not isinstance(patch.source_url, UnsetValue)
    if not _has_version_patch(patch) and not has_document_patch:
        raise ValidationError("Metadata patch contains no changes")
    return replace(
        document,
        title=patch.title if patch.title is not None else document.title,
        document_type=(
            patch.document_type if patch.document_type is not None else document.document_type
        ),
        owner_department=(
            patch.owner_department
            if patch.owner_department is not None
            else document.owner_department
        ),
        document_owner=(
            patch.document_owner if patch.document_owner is not None else document.document_owner
        ),
        confidentiality=(
            patch.confidentiality if patch.confidentiality is not None else document.confidentiality
        ),
        status=patch.status if patch.status is not None else document.status,
        language=patch.language if patch.language is not None else document.language,
        external_llm_allowed=(
            patch.external_llm_allowed
            if patch.external_llm_allowed is not None
            else document.external_llm_allowed
        ),
        source_path=patch.source_path if patch.source_path is not None else document.source_path,
        source_url=(
            document.source_url if isinstance(patch.source_url, UnsetValue) else patch.source_url
        ),
        custom_metadata=(
            dict(patch.custom_metadata)
            if patch.custom_metadata is not None
            else document.custom_metadata
        ),
        acl=list(patch.acl) if patch.acl is not None else document.acl,
        updated_at=utc_now(),
    )


def _has_version_patch(patch: MetadataPatch) -> bool:
    return (
        patch.version_number is not None
        or patch.source_version is not None
        or not isinstance(patch.effective_at, UnsetValue)
        or not isinstance(patch.expires_at, UnsetValue)
        or not isinstance(patch.review_at, UnsetValue)
    )


def _apply_version_patch(version: DocumentVersion, patch: MetadataPatch) -> DocumentVersion:
    return replace(
        version,
        version_number=(
            patch.version_number if patch.version_number is not None else version.version_number
        ),
        source_version=(
            patch.source_version if patch.source_version is not None else version.source_version
        ),
        effective_at=(
            version.effective_at
            if isinstance(patch.effective_at, UnsetValue)
            else patch.effective_at
        ),
        expires_at=(
            version.expires_at if isinstance(patch.expires_at, UnsetValue) else patch.expires_at
        ),
        review_at=(
            version.review_at if isinstance(patch.review_at, UnsetValue) else patch.review_at
        ),
    )


def _job_view(job: IndexingJob) -> JobView:
    return JobView(
        job_id=job.id,
        document_id=job.document_id,
        version_id=job.version_id,
        status=job.status,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        error_code=job.error_code,
        error_message=job.error_message,
    )


def _vector_payload(document: Document, chunk: Chunk, is_current: bool) -> dict[str, object]:
    """建立 Qdrant 所需的最小、可重建 filter payload。"""

    return {
        "chunk_id": str(chunk.id),
        "document_id": str(document.id),
        "version_id": str(chunk.version_id),
        "is_current": is_current,
        "status": document.status.value,
        "confidentiality": document.confidentiality.value,
        "language": document.language,
        "external_llm_allowed": document.external_llm_allowed,
        "acl_public": document.is_acl_public,
        "allowed_user_ids": document.allowed_user_ids,
        "allowed_group_ids": document.allowed_group_ids,
        "denied_user_ids": document.denied_user_ids,
        "denied_group_ids": document.denied_group_ids,
    }


def _new_index_job(document_id: UUID, version_id: UUID) -> tuple[IndexingJob, OutboxEvent]:
    """一起建立 job/event，確保兩者 ID 與 aggregate 關係一致。"""

    job = IndexingJob(
        id=uuid4(),
        job_type=JobType.INDEX_VERSION,
        document_id=document_id,
        version_id=version_id,
    )
    event = OutboxEvent(
        id=uuid4(),
        event_type=JobType.INDEX_VERSION,
        aggregate_id=document_id,
        job_id=job.id,
        payload={"document_id": str(document_id), "version_id": str(version_id)},
    )
    return job, event


def _document_matches_filters(
    document: Document,
    version: DocumentVersion,
    chunk: Chunk,
    filters: SearchFilters,
    today: date,
) -> bool:
    """以 PostgreSQL canonical entity 做最終 current/date/status/ACL 驗證。"""

    if (
        document.deleted_at is not None
        or document.current_version_id != chunk.version_id
        or version.id != chunk.version_id
    ):
        return False
    if version.effective_at and version.effective_at > today:
        return False
    if version.expires_at and version.expires_at < today:
        return False
    if filters.statuses and document.status not in filters.statuses:
        return False
    if filters.confidentialities and document.confidentiality not in filters.confidentialities:
        return False
    if filters.language and document.language != filters.language:
        return False
    if filters.user_id and filters.user_id in document.denied_user_ids:
        return False
    if set(filters.group_ids).intersection(document.denied_group_ids):
        return False
    if document.is_acl_public:
        return True
    if filters.user_id and filters.user_id in document.allowed_user_ids:
        return True
    return bool(set(filters.group_ids).intersection(document.allowed_group_ids))


def _search_token_count(chunk: Chunk) -> int:
    if chunk.token_count is not None:
        return chunk.token_count
    # Legacy chunks may predate mandatory token counts. UTF-8 byte length is a
    # deterministic, deliberately conservative context-budget estimate for both
    # ASCII and multibyte company documents.
    return max(1, len(chunk.content.encode("utf-8")))


def _safe_error_message(exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message[:2000]

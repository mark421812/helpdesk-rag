"""Qdrant vector-store adapter。

Qdrant 只保存可重建的向量與最小 filter payload；完整 chunk 與 canonical metadata 仍以
PostgreSQL 為準。此 class 實作 Application 的 VectorStorePort。
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from uuid import UUID

from qdrant_client import AsyncQdrantClient, models

from knowledge_index_service.domain.entities import (
    Document,
    SearchFilters,
    VectorPoint,
    VectorSearchHit,
)
from knowledge_index_service.domain.exceptions import ConflictError, ServiceNotReadyError
from knowledge_index_service.infrastructure.config import Settings


class QdrantVectorStore:
    """以 qdrant-client 實作 VectorStorePort。"""

    def __init__(self, settings: Settings) -> None:
        model_fingerprint = sha256(
            f"{settings.model_id}@{settings.model_revision}".encode()
        ).hexdigest()[:12]
        self._collection = f"{settings.qdrant_collection}_{model_fingerprint}"
        self._vectors_on_disk = settings.qdrant_vectors_on_disk
        self._hnsw_on_disk = settings.qdrant_hnsw_on_disk
        self._payload_on_disk = settings.qdrant_payload_on_disk
        self._client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=30,
        )

    async def ensure_collection(self, dimension: int) -> None:
        """確認 collection 存在且向量維度與目前 pinned model 相符。"""

        try:
            await self._ensure_collection(dimension)
        except ConflictError:
            raise
        except Exception as exc:
            raise ServiceNotReadyError("Qdrant is unavailable") from exc

    async def _ensure_collection(self, dimension: int) -> None:
        if await self._client.collection_exists(self._collection):
            collection = await self._client.get_collection(self._collection)
            configured = collection.config.params.vectors
            if isinstance(configured, models.VectorParams) and configured.size != dimension:
                raise ConflictError(
                    f"Qdrant collection dimension is {configured.size}, expected {dimension}"
                )
            return
        try:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=dimension,
                    distance=models.Distance.COSINE,
                    on_disk=self._vectors_on_disk,
                ),
                hnsw_config=models.HnswConfigDiff(on_disk=self._hnsw_on_disk),
                on_disk_payload=self._payload_on_disk,
            )
        except Exception:
            if await self._client.collection_exists(self._collection):
                return
            raise
        await self._create_payload_indexes()

    async def upsert(self, points: Sequence[VectorPoint]) -> None:
        """把 Application VectorPoint 轉成 Qdrant PointStruct 後可靠寫入。"""

        if not points:
            return
        await self._client.upsert(
            collection_name=self._collection,
            points=[
                models.PointStruct(
                    id=point.point_id,
                    vector=point.vector,
                    payload=point.payload,
                )
                for point in points
            ],
            wait=True,
        )

    async def search(
        self,
        vector: Sequence[float],
        limit: int,
        filters: SearchFilters,
        score_threshold: float | None,
        offset: int = 0,
    ) -> list[VectorSearchHit]:
        """執行 cosine vector search，並使用 payload filter 做第一層 ACL 篩選。"""

        try:
            result = await self._client.query_points(
                collection_name=self._collection,
                query=list(vector),
                query_filter=_search_filter(filters),
                limit=limit,
                offset=offset,
                score_threshold=score_threshold,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise ServiceNotReadyError("Qdrant search is unavailable") from exc
        return [
            VectorSearchHit(
                point_id=str(point.id),
                score=point.score,
                payload=dict(point.payload or {}),
            )
            for point in result.points
        ]

    async def deactivate_document_versions(
        self,
        document_id: UUID,
        active_version_id: UUID,
    ) -> None:
        await self._client.set_payload(
            collection_name=self._collection,
            payload={"is_current": False},
            points=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchValue(value=str(document_id)),
                        )
                    ],
                    must_not=[
                        models.FieldCondition(
                            key="version_id",
                            match=models.MatchValue(value=str(active_version_id)),
                        )
                    ],
                )
            ),
            wait=True,
        )

    async def sync_document_payload(self, document: Document) -> None:
        await self._client.set_payload(
            collection_name=self._collection,
            payload={
                "status": document.status.value,
                "confidentiality": document.confidentiality.value,
                "language": document.language,
                "external_llm_allowed": document.external_llm_allowed,
                "acl_public": document.is_acl_public,
                "allowed_user_ids": document.allowed_user_ids,
                "allowed_group_ids": document.allowed_group_ids,
                "denied_user_ids": document.denied_user_ids,
                "denied_group_ids": document.denied_group_ids,
            },
            points=models.FilterSelector(filter=_document_filter(document.id)),
            wait=True,
        )

    async def delete_document(self, document_id: UUID) -> None:
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(filter=_document_filter(document_id)),
            wait=True,
        )

    async def is_ready(self) -> bool:
        try:
            await self._client.get_collections()
        except Exception:
            return False
        return True

    async def close(self) -> None:
        await self._client.close()

    async def _create_payload_indexes(self) -> None:
        keyword_fields = (
            "document_id",
            "version_id",
            "status",
            "confidentiality",
            "language",
            "allowed_user_ids",
            "allowed_group_ids",
            "denied_user_ids",
            "denied_group_ids",
        )
        for field in keyword_fields:
            await self._client.create_payload_index(
                collection_name=self._collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
                wait=True,
            )
        for field in ("is_current", "acl_public", "external_llm_allowed"):
            await self._client.create_payload_index(
                collection_name=self._collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.BOOL,
                wait=True,
            )


def _document_filter(document_id: UUID) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(
                key="document_id",
                match=models.MatchValue(value=str(document_id)),
            )
        ]
    )


def _search_filter(filters: SearchFilters) -> models.Filter:
    """把 Domain SearchFilters 映射成 Qdrant must/should/must_not filter tree。"""

    must: list[models.Condition] = [
        models.FieldCondition(key="is_current", match=models.MatchValue(value=True)),
    ]
    if filters.statuses:
        must.append(
            models.FieldCondition(
                key="status",
                match=models.MatchAny(any=[value.value for value in filters.statuses]),
            )
        )
    if filters.confidentialities:
        must.append(
            models.FieldCondition(
                key="confidentiality",
                match=models.MatchAny(any=[value.value for value in filters.confidentialities]),
            )
        )
    if filters.language:
        must.append(
            models.FieldCondition(
                key="language",
                match=models.MatchValue(value=filters.language),
            )
        )

    acl_conditions: list[models.Condition] = [
        models.FieldCondition(key="acl_public", match=models.MatchValue(value=True))
    ]
    if filters.user_id:
        acl_conditions.append(
            models.FieldCondition(
                key="allowed_user_ids",
                match=models.MatchValue(value=filters.user_id),
            )
        )
    if filters.group_ids:
        acl_conditions.append(
            models.FieldCondition(
                key="allowed_group_ids",
                match=models.MatchAny(any=list(filters.group_ids)),
            )
        )
    must.append(models.Filter(should=acl_conditions))

    must_not: list[models.Condition] = []
    if filters.user_id:
        must_not.append(
            models.FieldCondition(
                key="denied_user_ids",
                match=models.MatchValue(value=filters.user_id),
            )
        )
    if filters.group_ids:
        must_not.append(
            models.FieldCondition(
                key="denied_group_ids",
                match=models.MatchAny(any=list(filters.group_ids)),
            )
        )
    return models.Filter(must=must, must_not=must_not)

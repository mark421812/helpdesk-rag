"""KIS HTTP routes。

Route 的責任是把 Pydantic request schema 映射成 Application DTO，呼叫 use case，再把結果
映射成 response schema；它不應直接寫 SQL 或呼叫 Qdrant。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Request, status

from knowledge_index_service.application.dtos import (
    UNSET,
    ChunkSubmission,
    DocumentSubmission,
    MetadataPatch,
    SearchRequest,
)
from knowledge_index_service.bootstrap import Container
from knowledge_index_service.domain.entities import AccessControlEntry, SearchFilters
from knowledge_index_service.presentation.schemas import (
    DocumentSubmissionRequest,
    DocumentSubmissionResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    JobResponse,
    MetadataPatchRequest,
    SearchRequestBody,
    SearchResponse,
    SearchResultItemResponse,
    ServiceAuthResponse,
)

router = APIRouter(prefix="/api/v1")


def _container(request: Request) -> Container:
    """從 FastAPI app.state 取出 lifespan 建立的 Container，並提供型別資訊。"""

    return cast(Container, request.app.state.container)


@router.get("/auth/check", response_model=ServiceAuthResponse)
async def check_service_authentication() -> ServiceAuthResponse:
    """能進入 route 就代表 API authentication middleware 已放行。"""

    return ServiceAuthResponse(status="ok")


@router.post(
    "/documents",
    response_model=DocumentSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_document(
    body: DocumentSubmissionRequest,
    request: Request,
) -> DocumentSubmissionResponse:
    """將 HTTP schema 映射成 Application DTO，再執行提交 use case。"""

    result = await _container(request).submit_document.execute(
        DocumentSubmission(
            source_type=body.source_type,
            source_item_id=body.source_item_id,
            source_version=body.source_version,
            title=body.title,
            document_type=body.document_type,
            owner_department=body.owner_department,
            document_owner=body.document_owner,
            confidentiality=body.confidentiality,
            status=body.status,
            language=body.language,
            external_llm_allowed=body.external_llm_allowed,
            version_number=body.version_number,
            content_hash=body.content_hash.lower(),
            mime_type=body.mime_type,
            file_size=body.file_size,
            parser_name=body.parser_name,
            parser_version=body.parser_version,
            # Pydantic list 轉成 Domain/Application 偏好的 immutable tuple。
            chunks=tuple(
                ChunkSubmission(
                    content=item.content,
                    section_path=item.section_path,
                    page_number=item.page_number,
                    token_count=item.token_count,
                )
                for item in body.chunks
            ),
            source_path=body.source_path,
            source_url=body.source_url,
            effective_at=body.effective_at,
            expires_at=body.expires_at,
            review_at=body.review_at,
            custom_metadata=body.custom_metadata,
            acl=tuple(
                AccessControlEntry(
                    principal_type=item.principal_type,
                    principal_id=item.principal_id,
                    effect=item.effect,
                )
                for item in body.acl
            ),
        )
    )
    return DocumentSubmissionResponse(**asdict(result))


@router.post("/embeddings", response_model=EmbeddingResponse)
async def embed_texts(body: EmbeddingRequest, request: Request) -> EmbeddingResponse:
    """將 HTTP embedding request 映射到 EmbedTexts use case。"""

    result = await _container(request).embed_texts.execute(body.texts, body.input_type)
    return EmbeddingResponse(
        vectors=[list(vector) for vector in result.vectors],
        model_id=result.model_id,
        model_revision=result.model_revision,
        dimension=result.dimension,
    )


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequestBody, request: Request) -> SearchResponse:
    """執行 query embedding、vector search 與 canonical hydration。"""

    result = await _container(request).search_knowledge.execute(
        SearchRequest(
            query=body.query,
            limit=body.limit,
            score_threshold=body.score_threshold,
            filters=SearchFilters(
                user_id=body.filters.user_id,
                group_ids=tuple(body.filters.group_ids),
                statuses=tuple(body.filters.statuses),
                confidentialities=tuple(body.filters.confidentialities),
                language=body.filters.language,
            ),
        )
    )
    return SearchResponse(
        items=[SearchResultItemResponse(**asdict(item)) for item in result.items],
        model_id=result.model_id,
        model_revision=result.model_revision,
    )


@router.patch(
    "/documents/{document_id}/metadata",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def update_metadata(
    document_id: UUID,
    body: MetadataPatchRequest,
    request: Request,
) -> JobResponse:
    """PATCH 必須用 model_fields_set 區分 omitted 與 explicit null。"""

    result = await _container(request).update_metadata.execute(
        document_id,
        MetadataPatch(
            title=body.title,
            document_type=body.document_type,
            owner_department=body.owner_department,
            document_owner=body.document_owner,
            confidentiality=body.confidentiality,
            status=body.status,
            language=body.language,
            external_llm_allowed=body.external_llm_allowed,
            source_path=body.source_path,
            source_url=(body.source_url if "source_url" in body.model_fields_set else UNSET),
            custom_metadata=body.custom_metadata,
            version_number=body.version_number,
            source_version=body.source_version,
            effective_at=(body.effective_at if "effective_at" in body.model_fields_set else UNSET),
            expires_at=(body.expires_at if "expires_at" in body.model_fields_set else UNSET),
            review_at=(body.review_at if "review_at" in body.model_fields_set else UNSET),
            acl=(
                tuple(
                    AccessControlEntry(
                        principal_type=item.principal_type,
                        principal_id=item.principal_id,
                        effect=item.effect,
                    )
                    for item in body.acl
                )
                if body.acl is not None
                else None
            ),
        ),
    )
    return JobResponse(**asdict(result))


@router.post(
    "/documents/{document_id}/reindex",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reindex_document(document_id: UUID, request: Request) -> JobResponse:
    """為目前版本排入重新索引工作。"""

    result = await _container(request).reindex_document.execute(document_id)
    return JobResponse(**asdict(result))


@router.delete(
    "/documents/{document_id}",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_document(document_id: UUID, request: Request) -> JobResponse:
    """Soft delete canonical document 並排入 vector cleanup。"""

    result = await _container(request).delete_document.execute(document_id)
    return JobResponse(**asdict(result))


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: UUID, request: Request) -> JobResponse:
    """查詢一筆 KIS background job。"""

    result = await _container(request).get_job.execute(job_id)
    return JobResponse(**asdict(result))

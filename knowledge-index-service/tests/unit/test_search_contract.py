from __future__ import annotations

from uuid import uuid4

from knowledge_index_service.domain.enums import Confidentiality, DocumentStatus
from knowledge_index_service.presentation.schemas import (
    SearchRequestBody,
    SearchResponse,
    ServiceAuthResponse,
)


def test_search_request_keeps_empty_statuses_api_compatible() -> None:
    request = SearchRequestBody.model_validate(
        {
            "query": "遠端工作規章",
            "filters": {"statuses": []},
        }
    )

    assert request.filters.statuses == []


def test_service_auth_response_contract() -> None:
    assert ServiceAuthResponse(status="ok").model_dump() == {"status": "ok"}


def test_search_response_exposes_canonical_policy_and_chunk_fields() -> None:
    chunk_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()

    response = SearchResponse.model_validate(
        {
            "items": [
                {
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "version_id": version_id,
                    "score": 0.87,
                    "content": "員工每週可以遠端工作兩天。",
                    "title": "遠端工作規章",
                    "document_type": "policy",
                    "source_path": "/documents/remote-work.md",
                    "source_url": None,
                    "section_path": "遠端工作",
                    "page_number": None,
                    "token_count": 17,
                    "chunk_index": 0,
                    "version_number": "1.0",
                    "effective_at": None,
                    "expires_at": None,
                    "external_llm_allowed": False,
                    "confidentiality": Confidentiality.INTERNAL,
                    "language": "zh-TW",
                    "status": DocumentStatus.PUBLISHED,
                    "custom_metadata": {},
                }
            ],
            "model_id": "intfloat/multilingual-e5-small",
            "model_revision": "test-revision",
        }
    )

    item = response.items[0]
    assert item.chunk_id == chunk_id
    assert item.external_llm_allowed is False
    assert item.confidentiality == Confidentiality.INTERNAL
    assert item.language == "zh-TW"
    assert item.status == DocumentStatus.PUBLISHED
    assert item.token_count == 17
    assert item.chunk_index == 0

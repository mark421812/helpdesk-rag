from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from retrieval_service.application.dtos import KnowledgeSearchCriteria
from retrieval_service.domain.entities import PrincipalContext
from retrieval_service.domain.enums import DocumentStatus
from retrieval_service.domain.exceptions import (
    DependencyContractError,
    DependencyTimeoutError,
)
from retrieval_service.infrastructure.knowledge_index.http_client import (
    KnowledgeIndexHttpClient,
)
from retrieval_service.infrastructure.request_context import request_id_context


def _search_response(**item_overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "chunk_id": str(uuid4()),
        "document_id": str(uuid4()),
        "version_id": str(uuid4()),
        "score": 0.88,
        "content": "員工每週可以遠端工作兩天。",
        "title": "遠端工作規章",
        "document_type": "policy",
        "source_path": "/private/company/policy.pdf",
        "source_url": None,
        "section_path": "遠端工作",
        "page_number": 3,
        "version_number": "1",
        "effective_at": None,
        "expires_at": None,
        "custom_metadata": {},
        "confidentiality": "internal",
        "status": "published",
        "language": "zh-TW",
        "external_llm_allowed": False,
        "token_count": 15,
        "chunk_index": 0,
    }
    item.update(item_overrides)
    return {
        "items": [item],
        "model_id": "intfloat/multilingual-e5-small",
        "model_revision": "test-revision",
    }


def _criteria() -> KnowledgeSearchCriteria:
    return KnowledgeSearchCriteria(
        query="遠端工作規定",
        limit=12,
        principal=PrincipalContext(
            user_id="entra-user-id",
            group_ids=("all-employees",),
        ),
        language="zh-TW",
        score_threshold=None,
    )


@pytest.mark.asyncio
async def test_search_forwards_trusted_acl_and_forces_published() -> None:
    captured: dict[str, Any] = {}
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        captured_headers.update(request.headers)
        return httpx.Response(200, json=_search_response())

    client = KnowledgeIndexHttpClient(
        "http://knowledge-index.test",
        api_key="internal-key",
        transport=httpx.MockTransport(handler),
    )
    request_id_token = request_id_context.set("request-123")
    try:
        result = await client.search(_criteria())
    finally:
        request_id_context.reset(request_id_token)
        await client.close()

    filters = captured["filters"]
    assert filters["user_id"] == "entra-user-id"
    assert filters["group_ids"] == ["all-employees"]
    assert filters["statuses"] == [DocumentStatus.PUBLISHED.value]
    assert captured_headers["x-internal-api-key"] == "internal-key"
    assert captured_headers["x-request-id"] == "request-123"
    assert result.candidates[0].external_llm_allowed is False
    assert result.candidates[0].confidentiality.value == "internal"
    assert not hasattr(result.candidates[0], "source_path")
    assert not hasattr(result.candidates[0], "source_url")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_llm_allowed", None),
        ("token_count", None),
        ("status", "draft"),
    ],
)
async def test_search_rejects_unsafe_or_malformed_contract(field: str, value: object) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=_search_response(**{field: value}))
    )
    client = KnowledgeIndexHttpClient(
        "http://knowledge-index.test",
        transport=transport,
    )
    try:
        with pytest.raises(DependencyContractError):
            await client.search(_criteria())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_timeout_is_retried_once_then_mapped() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = KnowledgeIndexHttpClient(
        "http://knowledge-index.test",
        retry_attempts=1,
        retry_backoff_seconds=0,
        retry_max_backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DependencyTimeoutError):
            await client.search(_criteria())
    finally:
        await client.close()
    assert attempts == 2


@pytest.mark.asyncio
async def test_retryable_status_is_retried_within_the_bounded_attempt_count() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json=_search_response())

    client = KnowledgeIndexHttpClient(
        "http://knowledge-index.test",
        retry_attempts=1,
        retry_backoff_seconds=0,
        retry_max_backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.search(_criteria())
    finally:
        await client.close()

    assert len(result.candidates) == 1
    assert attempts == 2


@pytest.mark.asyncio
async def test_streamed_response_is_stopped_at_the_decompressed_body_limit() -> None:
    client = KnowledgeIndexHttpClient(
        "http://knowledge-index.test",
        max_response_body_bytes=50,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"{" + (b"x" * 100) + b"}")
        ),
    )
    try:
        with pytest.raises(DependencyContractError, match="too large"):
            await client.search(_criteria())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_readiness_requires_all_knowledge_index_checks() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "status": "ok",
                "checks": {"postgres": True, "qdrant": True, "embedding_model": True},
            },
        )
    )
    client = KnowledgeIndexHttpClient(
        "http://knowledge-index.test",
        transport=transport,
    )
    try:
        assert await client.is_ready() is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_readiness_fails_when_service_api_key_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "checks": {
                        "postgres": True,
                        "qdrant": True,
                        "embedding_model": True,
                    },
                },
            )
        return httpx.Response(401, json={"code": "unauthorized"})

    client = KnowledgeIndexHttpClient(
        "http://knowledge-index.test",
        api_key="wrong-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.is_ready() is False
    finally:
        await client.close()

from __future__ import annotations

import json
from datetime import date
from uuid import uuid4

import httpx
import pytest

from ingestion_service.application.dtos import (
    KnowledgeIndexDocumentSubmission,
    KnowledgeIndexMetadataPatch,
)
from ingestion_service.domain.entities import AccessControlEntry, Chunk
from ingestion_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    PermissionEffect,
    PrincipalType,
)
from ingestion_service.domain.exceptions import ExternalServiceError, ValidationError
from ingestion_service.infrastructure.knowledge_index.http_client import (
    KnowledgeIndexHttpClient,
)


@pytest.mark.asyncio
async def test_submit_document_maps_contract_and_internal_api_key() -> None:
    document_id = uuid4()
    version_id = uuid4()
    job_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-internal-api-key"] == "secret"
        payload = json.loads(request.content)
        assert payload["source_item_id"].startswith("local:")
        assert payload["version_number"] == "3"
        assert payload["chunks"][0]["page_number"] == 2
        return httpx.Response(
            202,
            json={
                "document_id": str(document_id),
                "version_id": str(version_id),
                "job_id": str(job_id),
                "indexing_status": "pending",
                "created": True,
            },
        )

    client = KnowledgeIndexHttpClient(
        "http://knowledge-index",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )
    submission = KnowledgeIndexDocumentSubmission(
        source_type="local_folder",
        source_item_id=f"local:{uuid4()}:abc",
        source_version="mtime:1;size:10",
        title="Policy",
        document_type="policy",
        owner_department="HR",
        document_owner="hr@example.com",
        confidentiality=Confidentiality.INTERNAL,
        status=DocumentStatus.PUBLISHED,
        language="zh-TW",
        external_llm_allowed=False,
        version_number="3",
        content_hash="a" * 64,
        mime_type="text/plain",
        file_size=10,
        parser_name="text",
        parser_version="1",
        chunks=(Chunk("content", "Rules", 2, 1),),
        acl=(AccessControlEntry(PrincipalType.GROUP, "employees"),),
    )

    result = await client.submit_document(submission)
    await client.close()

    assert result.document_id == document_id
    assert result.version_id == version_id
    assert result.job_id == job_id


@pytest.mark.asyncio
async def test_metadata_patch_sends_explicit_null_dates_and_acl() -> None:
    document_id = uuid4()
    job_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["effective_at"] == "2026-08-02"
        assert payload["expires_at"] is None
        assert payload["review_at"] is None
        assert payload["acl"] == [
            {"principal_type": "group", "principal_id": "hr", "effect": "deny"}
        ]
        return httpx.Response(
            202,
            json={
                "job_id": str(job_id),
                "document_id": str(document_id),
                "version_id": None,
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 5,
                "error_code": None,
                "error_message": None,
            },
        )

    client = KnowledgeIndexHttpClient(
        "http://knowledge-index",
        transport=httpx.MockTransport(handler),
    )
    patch = KnowledgeIndexMetadataPatch(
        source_version="source-v2",
        version_number="2",
        title="Policy",
        document_type="policy",
        owner_department="HR",
        document_owner="hr@example.com",
        confidentiality=Confidentiality.INTERNAL,
        status=DocumentStatus.PUBLISHED,
        language="zh-TW",
        external_llm_allowed=False,
        source_path="policy.md",
        source_url=None,
        effective_at=date(2026, 8, 2),
        expires_at=None,
        review_at=None,
        custom_metadata={},
        acl=(
            AccessControlEntry(
                PrincipalType.GROUP,
                "hr",
                PermissionEffect.DENY,
            ),
        ),
    )

    result = await client.patch_metadata(document_id, patch)
    await client.close()

    assert result.job_id == job_id


@pytest.mark.asyncio
async def test_capacity_rejection_is_not_retryable() -> None:
    client = KnowledgeIndexHttpClient(
        "http://knowledge-index",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(413, json={"message": "too large"})
        ),
    )

    with pytest.raises(ExternalServiceError) as captured:
        await client.delete_document(uuid4())
    await client.close()

    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_request_is_rejected_locally_when_encoded_json_exceeds_kis_limit() -> None:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Oversized request reached the transport: {request.url}")

    client = KnowledgeIndexHttpClient(
        "http://knowledge-index",
        max_request_body_bytes=32,
        transport=httpx.MockTransport(unexpected_request),
    )

    with pytest.raises(ValidationError, match="request body"):
        await client._request(
            "POST",
            "/api/v1/documents",
            json={"content": "公司規章" * 20},
            expected_status=202,
        )
    await client.close()


@pytest.mark.asyncio
async def test_readiness_uses_independent_short_timeout() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        timeout = request.extensions["timeout"]
        assert timeout == {
            "connect": 2.5,
            "read": 2.5,
            "write": 2.5,
            "pool": 2.5,
        }
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"status": "ok"})

    client = KnowledgeIndexHttpClient(
        "http://knowledge-index",
        timeout_seconds=120,
        readiness_timeout_seconds=2.5,
        transport=httpx.MockTransport(handler),
    )

    assert await client.is_ready()
    assert requested_paths == ["/health/ready", "/api/v1/auth/check"]
    await client.close()


@pytest.mark.asyncio
async def test_readiness_fails_when_knowledge_index_api_key_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ok"})
        assert request.headers["x-internal-api-key"] == "wrong-key"
        return httpx.Response(401, json={"error": "unauthorized"})

    client = KnowledgeIndexHttpClient(
        "http://knowledge-index",
        api_key="wrong-key",
        transport=httpx.MockTransport(handler),
    )

    assert await client.is_ready() is False
    await client.close()

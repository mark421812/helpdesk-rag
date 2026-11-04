from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from retrieval_service.application.dtos import (
    KnowledgeSearchCriteria,
    KnowledgeSearchResult,
    RetrievalPolicy,
)
from retrieval_service.application.use_cases import RetrieveEvidence
from retrieval_service.bootstrap import Runtime
from retrieval_service.domain.entities import CandidateChunk
from retrieval_service.domain.enums import Confidentiality
from retrieval_service.infrastructure.config import Settings
from retrieval_service.infrastructure.identity.development import (
    DevelopmentHeaderIdentityProvider,
)
from retrieval_service.presentation.app import create_app


class FakeKnowledgeSearch:
    def __init__(self, candidates: tuple[CandidateChunk, ...]) -> None:
        self.candidates = candidates
        self.criteria: KnowledgeSearchCriteria | None = None

    async def search(self, criteria: KnowledgeSearchCriteria) -> KnowledgeSearchResult:
        self.criteria = criteria
        return KnowledgeSearchResult(
            candidates=self.candidates,
            model_id="test-model",
            model_revision="test-revision",
        )

    async def is_ready(self) -> bool:
        return True


class FakeContextAttestor:
    """API tests 不需要真實私鑰，但仍要驗證簽章呼叫邊界。"""

    def __init__(self) -> None:
        self.issue_count = 0

    def issue(self, query: object, evidence: tuple[object, ...]) -> str:
        self.issue_count += 1
        assert evidence
        return "test.header.signature"

    def is_ready(self) -> bool:
        return True


def _candidate(*, external_llm_allowed: bool) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        score=0.9,
        content="只能顯示給有權限同仁的公司規章。",
        title="機密作業規章",
        document_type="policy",
        section_path="權限",
        page_number=2,
        confidentiality=Confidentiality.RESTRICTED,
        language="zh-TW",
        external_llm_allowed=external_llm_allowed,
        token_count=20,
        chunk_index=0,
    )


def _app(
    search: FakeKnowledgeSearch,
    *,
    api_max_request_body_bytes: int = 65_536,
) -> tuple[FastAPI, Settings]:
    settings = Settings(
        _env_file=None,
        api_internal_key="retrieval-key",
        default_retrieval_limit=8,
        api_max_request_body_bytes=api_max_request_body_bytes,
    )
    runtime = Runtime(
        knowledge_search=search,
        identity_provider=DevelopmentHeaderIdentityProvider(),
        context_attestor=FakeContextAttestor(),
        retrieve_evidence=RetrieveEvidence(
            search,
            RetrievalPolicy(
                candidate_multiplier=3,
                max_candidate_limit=50,
                max_context_tokens=3_000,
                max_chunks_per_document=3,
            ),
        ),
    )
    return create_app(settings=settings, runtime=runtime), settings


@pytest.mark.asyncio
async def test_restricted_evidence_never_enters_external_llm_context() -> None:
    search = FakeKnowledgeSearch((_candidate(external_llm_allowed=False),))
    application, _ = _app(search)
    transport = httpx.ASGITransport(app=application)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/retrieve",
                json={"query": "機密作業規章"},
                headers={
                    "x-internal-api-key": "retrieval-key",
                    "x-dev-user-id": "employee-1",
                    "x-dev-group-ids": "restricted-team",
                },
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "external_generation_blocked"
    assert payload["llm_context"] == []
    assert len(payload["restricted_evidence"]) == 1
    assert payload["external_generation_blocked"] is True
    assert payload["context_attestation"] is None
    assert "source_path" not in response.text
    assert search.criteria is not None
    assert search.criteria.status.value == "published"
    assert search.criteria.principal.user_id == "employee-1"
    assert search.criteria.principal.group_ids == ("restricted-team",)


@pytest.mark.asyncio
async def test_allowed_context_is_never_returned_unsigned() -> None:
    search = FakeKnowledgeSearch((_candidate(external_llm_allowed=True),))
    application, _ = _app(search)
    transport = httpx.ASGITransport(app=application)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/retrieve",
                json={"query": "可外送的規章", "language": "zh-TW"},
                headers={
                    "x-internal-api-key": "retrieval-key",
                    "x-dev-user-id": "employee-1",
                },
            )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["llm_context"]) == 1
    assert payload["context_attestation"] == "test.header.signature"


@pytest.mark.asyncio
async def test_api_rejects_missing_internal_service_key() -> None:
    search = FakeKnowledgeSearch(())
    application, _ = _app(search)
    transport = httpx.ASGITransport(app=application)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/retrieve",
                json={"query": "公司規章"},
                headers={"x-dev-user-id": "employee-1"},
            )

    assert response.status_code == 401
    assert search.criteria is None


@pytest.mark.asyncio
async def test_api_rejects_missing_end_user_principal() -> None:
    search = FakeKnowledgeSearch(())
    application, _ = _app(search)
    transport = httpx.ASGITransport(app=application)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/retrieve",
                json={"query": "公司規章"},
                headers={"x-internal-api-key": "retrieval-key"},
            )

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized_principal"
    assert search.criteria is None


@pytest.mark.asyncio
async def test_readiness_checks_real_knowledge_index_port() -> None:
    search = FakeKnowledgeSearch(())
    application, _ = _app(search)
    transport = httpx.ASGITransport(app=application)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == {
        "identity_provider": True,
        "knowledge_index": True,
        "context_attestor": True,
    }


@pytest.mark.asyncio
async def test_chunked_body_is_rejected_before_fastapi_buffers_it() -> None:
    search = FakeKnowledgeSearch(())
    application, _ = _app(search, api_max_request_body_bytes=16)
    transport = httpx.ASGITransport(app=application)

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"query":"'
        yield b"this body is too large"

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/retrieve",
                content=chunks(),
                headers={
                    "content-type": "application/json",
                    "x-internal-api-key": "retrieval-key",
                    "x-dev-user-id": "employee-1",
                    "x-request-id": "bounded-body-test",
                },
            )

    assert response.status_code == 413
    assert response.headers["x-request-id"] == "bounded-body-test"
    assert response.json()["request_id"] == "bounded-body-test"
    assert search.criteria is None


@pytest.mark.asyncio
async def test_unmatched_urls_use_one_bounded_metrics_label() -> None:
    search = FakeKnowledgeSearch(())
    application, _ = _app(search)
    transport = httpx.ASGITransport(app=application)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/untrusted/random-one")
            await client.get("/untrusted/random-two")
            metrics = await client.get("/metrics")

    assert metrics.status_code == 200
    assert 'path="__unmatched__"' in metrics.text
    assert "/untrusted/random-one" not in metrics.text
    assert "/untrusted/random-two" not in metrics.text


@pytest.mark.asyncio
async def test_cors_preflight_is_answered_before_service_key_authentication() -> None:
    search = FakeKnowledgeSearch(())
    settings = Settings(
        _env_file=None,
        api_internal_key="retrieval-key",
        cors_origins=["http://helpdesk.local"],
    )
    runtime = Runtime(
        knowledge_search=search,
        identity_provider=DevelopmentHeaderIdentityProvider(),
        context_attestor=FakeContextAttestor(),
        retrieve_evidence=RetrieveEvidence(
            search,
            RetrievalPolicy(
                candidate_multiplier=3,
                max_candidate_limit=50,
                max_context_tokens=3_000,
                max_chunks_per_document=3,
            ),
        ),
    )
    application = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=application)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            preflight = await client.options(
                "/api/v1/retrieve",
                headers={
                    "origin": "http://helpdesk.local",
                    "access-control-request-method": "POST",
                    "access-control-request-headers": (
                        "content-type,x-internal-api-key,x-dev-user-id"
                    ),
                },
            )
            unauthorized = await client.post(
                "/api/v1/retrieve",
                json={"query": "公司規章"},
                headers={
                    "origin": "http://helpdesk.local",
                    "x-dev-user-id": "employee-1",
                },
            )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://helpdesk.local"
    assert unauthorized.status_code == 401
    assert unauthorized.headers["access-control-allow-origin"] == "http://helpdesk.local"
    assert search.criteria is None

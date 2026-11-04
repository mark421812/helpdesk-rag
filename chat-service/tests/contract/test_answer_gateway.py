"""Answer Generation HTTP adapter 的 response/request contract tests。"""

from __future__ import annotations

import gzip
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from chat_service.application.dtos import AnswerGenerationRequest
from chat_service.domain.entities import AllowedEvidence, Citation, Governance
from chat_service.domain.enums import Confidentiality
from chat_service.infrastructure.answer_generation.http_client import (
    AnswerGenerationGatewayError,
    AnswerGenerationHttpClient,
)

JsonHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _allowed_evidence(*, rank: int = 1) -> AllowedEvidence:
    chunk_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()
    return AllowedEvidence(
        rank=rank,
        chunk_id=chunk_id,
        document_id=document_id,
        version_id=version_id,
        score=0.91,
        content="approved policy content",
        document_type="policy",
        token_count=4,
        citation=Citation(
            citation_id=f"R{rank}",
            chunk_id=chunk_id,
            document_id=document_id,
            version_id=version_id,
            title="Remote Work Policy",
            section_path="Working hours",
            page_number=3,
        ),
        governance=Governance(
            confidentiality=Confidentiality.INTERNAL,
            language="zh-TW",
            external_llm_allowed=True,
        ),
    )


def _request(
    evidence: tuple[AllowedEvidence, ...] | tuple[Any, ...] | None = None,
) -> AnswerGenerationRequest:
    return AnswerGenerationRequest(
        question="processed question",
        language="zh-TW",
        evidence=(_allowed_evidence(),) if evidence is None else evidence,  # type: ignore[arg-type]
        context_attestation="opaque-attestation",
        request_id="request-123",
    )


def _answer_citation(evidence: AllowedEvidence, *, citation_id: str = "C1") -> dict[str, Any]:
    return {
        "citation_id": citation_id,
        "chunk_id": str(evidence.chunk_id),
        "document_id": str(evidence.document_id),
        "version_id": str(evidence.version_id),
        "title": evidence.citation.title,
        "section_path": evidence.citation.section_path,
        "page_number": evidence.citation.page_number,
    }


def _answered_response(
    evidence: AllowedEvidence, *, reuse_citation: bool = False
) -> dict[str, Any]:
    parts = [{"text": "第一段回答", "citation_ids": ["C1"]}]
    if reuse_citation:
        parts.append({"text": "第二段回答", "citation_ids": ["C1"]})
    return {
        "status": "answered",
        "answer_parts": parts,
        "citations": [_answer_citation(evidence)],
        "generation": {
            "provider": "fake",
            "model": "fake-model",
            "prompt_version": "prompt-v1",
            "finish_reason": "stop",
            "prompt_tokens": 12,
            "completion_tokens": 8,
        },
    }


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    max_response_body_bytes: int = 50_000,
) -> tuple[AnswerGenerationHttpClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    )
    adapter = AnswerGenerationHttpClient(
        "http://answer.test",
        api_key="answer-secret",
        client=http_client,
        max_response_body_bytes=max_response_body_bytes,
        connect_timeout_seconds=1,
        read_timeout_seconds=2,
        total_timeout_seconds=3,
        readiness_timeout_seconds=1,
    )
    return adapter, http_client


async def _run_with_transport(
    handler: JsonHandler,
    callback: Callable[[AnswerGenerationHttpClient, list[httpx.Request]], Awaitable[None]],
    *,
    max_response_body_bytes: int = 50_000,
) -> None:
    captured: list[httpx.Request] = []

    async def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return await handler(request)

    adapter, http_client = _client(
        httpx.MockTransport(recording_handler),
        max_response_body_bytes=max_response_body_bytes,
    )
    try:
        await callback(adapter, captured)
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_answer_projects_only_allowlisted_evidence_and_preserves_attestation() -> None:
    evidence = _allowed_evidence()
    response_payload = _answered_response(evidence)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == httpx.URL("http://answer.test/api/v1/answers")
        assert request.headers["x-internal-api-key"] == "answer-secret"
        assert request.headers["x-request-id"] == "request-123"
        body = json.loads(request.content)
        assert set(body) == {"question", "language", "evidence", "context_attestation"}
        assert body["question"] == "processed question"
        assert body["language"] == "zh-TW"
        assert body["context_attestation"] == "opaque-attestation"
        assert body["evidence"] == [
            {
                "rank": 1,
                "chunk_id": str(evidence.chunk_id),
                "document_id": str(evidence.document_id),
                "version_id": str(evidence.version_id),
                "score": 0.91,
                "content": "approved policy content",
                "document_type": "policy",
                "token_count": 4,
                "citation": {
                    "title": "Remote Work Policy",
                    "section_path": "Working hours",
                    "page_number": 3,
                },
                "governance": {
                    "confidentiality": "internal",
                    "language": "zh-TW",
                    "external_llm_allowed": True,
                },
            }
        ]
        return httpx.Response(200, json=response_payload)

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        result = await adapter.generate(_request((evidence,)))
        assert result.status.value == "answered"
        assert result.answer_parts[0].citation_ids == ("C1",)
        assert result.citations[0].citation_id == "C1"
        assert result.diagnostics.provider == "fake"

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_allows_one_citation_reused_across_multiple_parts() -> None:
    evidence = _allowed_evidence()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_answered_response(evidence, reuse_citation=True))

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        result = await adapter.generate(_request((evidence,)))
        assert len(result.answer_parts) == 2
        assert result.answer_parts[0].citation_ids == result.answer_parts[1].citation_ids

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_unable_to_answer_is_a_valid_empty_business_result() -> None:
    evidence = _allowed_evidence()
    payload = _answered_response(evidence)
    payload.update({"status": "unable_to_answer", "answer_parts": [], "citations": []})

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        result = await adapter.generate(_request((evidence,)))
        assert result.status.value == "unable_to_answer"
        assert result.answer_parts == ()
        assert result.citations == ()

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_503_is_not_retried() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            json={
                "error": "provider_unavailable",
                "message": "busy",
                "request_id": "downstream-request",
            },
        )

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(_request())
        assert raised.value.code == "provider_unavailable"
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "downstream_code", "expected_code"),
    [
        (401, "unauthorized", "answer_auth_misconfigured"),
        (403, "context_attestation_invalid", "answer_context_rejected"),
        (403, "outbound_policy_blocked", "outbound_policy_blocked"),
        (413, "body_too_large", "answer_contract_error"),
        (422, "invalid_request", "answer_contract_error"),
        (500, "internal_error", "answer_service_error"),
        (502, "provider_contract_error", "provider_contract_error"),
        (504, "provider_timeout", "provider_timeout"),
        (418, "teapot", "answer_service_error"),
    ],
)
async def test_answer_statuses_are_typed_and_never_retried(
    status_code: int,
    downstream_code: str,
    expected_code: str,
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            json={
                "error": downstream_code,
                "message": "safe error",
                "request_id": "downstream-request",
            },
        )

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(_request())
        assert raised.value.code == expected_code
        assert raised.value.status_code == status_code
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_429_exposes_only_validated_retry_after() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "7"},
            json={
                "error": "generation_busy",
                "message": "busy",
                "request_id": "downstream-request",
            },
        )

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(_request())
        assert raised.value.code == "generation_busy"
        assert raised.value.retry_after_seconds == 7

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_invalid_content_type_is_contract_error_without_retry() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b"{}",
            headers={"content-type": "application/jsonp"},
        )

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(_request())
        assert raised.value.code == "answer_contract_error"
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_unknown_success_status_is_contract_error() -> None:
    evidence = _allowed_evidence()
    invalid = _answered_response(evidence)
    invalid["status"] = "unknown"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=invalid)

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(_request((evidence,)))
        assert raised.value.code == "answer_contract_error"

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_invalid_response_is_contract_error_without_raw_body() -> None:
    secret = "provider-secret-must-not-leak"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=("{" + secret + "}").encode(),
            headers={"content-type": "application/json"},
        )

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(_request())
        assert raised.value.code == "answer_contract_error"
        assert secret not in str(raised.value)

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_oversized_body_is_contract_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{" + b"x" * 100 + b"}",
            headers={"content-type": "application/json"},
        )

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(_request())
        assert raised.value.code == "answer_contract_error"

    await _run_with_transport(handler, assertion, max_response_body_bytes=32)


@pytest.mark.asyncio
async def test_answer_compressed_response_is_rejected_before_decode() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=gzip.compress(b"{}"),
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(_request())
        assert raised.value.code == "answer_contract_error"
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_readiness_is_get_only_and_requires_all_checks_true() -> None:
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={"status": "ok", "checks": {"process": True, "provider": False}},
        )

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        assert await adapter.is_ready() is False
        assert methods == ["GET"]

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_answer_readiness_fails_closed_on_malformed_response() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not-json", headers={"content-type": "application/json"}
        )

    async def assertion(
        adapter: AnswerGenerationHttpClient, _captured: list[httpx.Request]
    ) -> None:
        assert await adapter.is_ready() is False

    await _run_with_transport(handler, assertion)

"""Retrieval/Answer HTTP adapter 的跨服務 contract tests。

測試使用 ``httpx.MockTransport``，只模擬不可信的 HTTP boundary；它們不啟動 sibling
service，也不把 transport schema 當成 Chat Domain entity。每個 response 都故意包含完整
cross-field invariants，讓合法 mixed rank 與 citation reuse 不會被 adapter 誤判。
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from chat_service.application.dtos import RetrievalRequest
from chat_service.domain.entities import Principal
from chat_service.infrastructure.retrieval.http_client import (
    RetrievalGatewayError,
    RetrievalHttpClient,
)

JsonHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _principal() -> Principal:
    return Principal(
        issuer="https://issuer.example",
        subject="employee-1",
        group_ids=("group-a", "group-b"),
    )


def _request(*, query: str = "processed query", language: str | None = "zh-TW") -> RetrievalRequest:
    return RetrievalRequest(
        query=query,
        limit=8,
        language=language,
        principal=_principal(),
        request_id="request-123",
    )


def _evidence(*, rank: int, allowed: bool) -> dict[str, Any]:
    chunk_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()
    return {
        "rank": rank,
        "chunk_id": str(chunk_id),
        "document_id": str(document_id),
        "version_id": str(version_id),
        "score": 0.91 if allowed else 0.81,
        "content": "allowed policy content" if allowed else "restricted policy content",
        "document_type": "policy",
        "token_count": 4,
        "citation": {
            "title": "Remote Work Policy",
            "section_path": "Working hours",
            "page_number": 3,
        },
        "governance": {
            "confidentiality": "internal" if allowed else "restricted",
            "language": "zh-TW",
            "external_llm_allowed": allowed,
        },
    }


def _retrieval_response(
    *,
    status: str = "evidence_found",
    llm_context: list[dict[str, Any]] | None = None,
    restricted_evidence: list[dict[str, Any]] | None = None,
    blocked: bool = False,
    attestation: str | None = "opaque-attestation",
    candidate_count: int | None = None,
) -> dict[str, Any]:
    allowed = [] if llm_context is None else llm_context
    restricted = [] if restricted_evidence is None else restricted_evidence
    selected_count = len(allowed) + len(restricted)
    return {
        "status": status,
        "llm_context": allowed,
        "restricted_evidence": restricted,
        "external_generation_blocked": blocked,
        "context_attestation": attestation,
        "retrieval": {
            "candidate_count": selected_count if candidate_count is None else candidate_count,
            "selected_count": selected_count,
            "model_id": "retrieval-model",
            "model_revision": "revision-1",
        },
    }


def _json_response(status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _error_payload(code: str) -> dict[str, str]:
    return {
        "error": code,
        "message": "safe downstream message",
        "request_id": "downstream-request",
    }


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    max_response_body_bytes: int = 50_000,
) -> tuple[RetrievalHttpClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    )
    return RetrievalHttpClient(
        "http://retrieval.test",
        api_key="retrieval-secret",
        client=http_client,
        retry_backoff_seconds=0,
        max_response_body_bytes=max_response_body_bytes,
        connect_timeout_seconds=1,
        read_timeout_seconds=2,
        total_timeout_seconds=3,
        readiness_timeout_seconds=1,
    ), http_client


async def _run_with_transport(
    handler: JsonHandler,
    callback: Callable[[RetrievalHttpClient, list[httpx.Request]], Awaitable[None]],
    *,
    max_response_body_bytes: int = 50_000,
) -> None:
    captured: list[httpx.Request] = []

    async def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return await handler(request)

    transport = httpx.MockTransport(recording_handler)
    adapter, http_client = _client(transport, max_response_body_bytes=max_response_body_bytes)
    try:
        await callback(adapter, captured)
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_retrieval_projects_exact_request_and_legal_mixed_ranks() -> None:
    response_payload = _retrieval_response(
        llm_context=[_evidence(rank=2, allowed=True)],
        restricted_evidence=[_evidence(rank=1, allowed=False)],
        candidate_count=3,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == httpx.URL("http://retrieval.test/api/v1/retrieve")
        assert request.headers["x-internal-api-key"] == "retrieval-secret"
        assert request.headers["x-request-id"] == "request-123"
        assert request.headers["x-dev-user-id"] == "employee-1"
        assert request.headers["x-dev-group-ids"] == "group-a,group-b"
        assert json.loads(request.content) == {
            "query": "processed query",
            "limit": 8,
            "language": "zh-TW",
        }
        return _json_response(200, response_payload)

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        result = await adapter.retrieve(_request())
        assert result.status.value == "evidence_found"
        assert [item.rank for item in result.llm_context] == [2]
        assert [item.rank for item in result.restricted_evidence] == [1]
        assert not hasattr(result.restricted_evidence[0], "content")
        assert result.llm_context[0].content == "allowed policy content"
        assert result.context_attestation == "opaque-attestation"
        assert result.candidate_count == 3
        assert result.selected_count == 2

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
@pytest.mark.parametrize("transient_status", [503, 504])
async def test_retrieval_retries_one_transient_status_and_then_succeeds(
    transient_status: int,
) -> None:
    calls = 0
    payload = _retrieval_response(llm_context=[_evidence(rank=1, allowed=True)])

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _json_response(transient_status, _error_payload("temporarily_unavailable"))
        return _json_response(200, payload)

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        result = await adapter.retrieve(_request())
        assert result.status.value == "evidence_found"
        assert calls == 2

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_retries_one_connect_failure_and_then_succeeds() -> None:
    calls = 0
    payload = _retrieval_response(llm_context=[_evidence(rank=1, allowed=True)])

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection failed", request=request)
        return _json_response(200, payload)

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        result = await adapter.retrieve(_request())
        assert result.status.value == "evidence_found"
        assert calls == 2

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "expected_code"),
    [
        (httpx.WriteTimeout, "retrieval_timeout"),
        (httpx.PoolTimeout, "retrieval_timeout"),
        (httpx.ReadError, "retrieval_unavailable"),
    ],
)
async def test_retrieval_non_connect_transport_failures_are_not_retried(
    exception_type: type[httpx.RequestError],
    expected_code: str,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise exception_type("synthetic transport failure", request=request)

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == expected_code
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "downstream_code", "expected_code"),
    [
        (400, "bad_request", "retrieval_contract_error"),
        (401, "unauthorized", "retrieval_auth_misconfigured"),
        (401, "unauthorized_principal", "retrieval_identity_rejected"),
        (413, "body_too_large", "retrieval_contract_error"),
        (500, "internal_error", "retrieval_service_error"),
        (502, "bad_gateway", "retrieval_contract_error"),
        (418, "teapot", "retrieval_service_error"),
    ],
)
async def test_retrieval_non_transient_errors_are_typed_and_not_retried(
    status_code: int,
    downstream_code: str,
    expected_code: str,
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(status_code, _error_payload(downstream_code))

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == expected_code
        assert raised.value.status_code == status_code
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_invalid_content_type_is_contract_error_without_retry() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b"{}",
            headers={"content-type": "text/html"},
        )

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == "retrieval_contract_error"
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_malformed_json_is_contract_error_without_retry() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b"{not-json",
            headers={"content-type": "application/json; charset=utf-8"},
        )

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == "retrieval_contract_error"
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_transient_status_with_invalid_error_content_type_is_not_retried() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, content=b"temporary", headers={"content-type": "text/plain"})

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == "retrieval_unavailable"
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_oversized_body_is_contract_error_without_retry() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b"{" + b"x" * 100 + b"}",
            headers={"content-type": "application/json"},
        )

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == "retrieval_contract_error"
        assert calls == 1

    await _run_with_transport(handler, assertion, max_response_body_bytes=32)


@pytest.mark.asyncio
async def test_retrieval_compressed_response_is_rejected_before_decode() -> None:
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

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == "retrieval_contract_error"
        assert calls == 1

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_cross_field_invariant_failure_is_contract_error() -> None:
    invalid = _retrieval_response(
        llm_context=[_evidence(rank=1, allowed=True)],
        blocked=True,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(200, invalid)

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == "retrieval_contract_error"

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_unknown_success_status_is_contract_error() -> None:
    invalid = _retrieval_response(status="unknown", llm_context=[], attestation=None)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(200, invalid)

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == "retrieval_contract_error"

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_readiness_requires_all_checks_true_and_uses_get() -> None:
    captured_methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        return _json_response(
            200,
            {"status": "ok", "checks": {"process": True, "qdrant": False}},
        )

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        assert await adapter.is_ready() is False
        assert captured_methods == ["GET"]

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_readiness_fails_closed_on_malformed_response() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not-json", headers={"content-type": "application/json"}
        )

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        assert await adapter.is_ready() is False

    await _run_with_transport(handler, assertion)


@pytest.mark.asyncio
async def test_retrieval_redirect_is_not_followed() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            307,
            headers={"location": "http://untrusted.test/api/v1/retrieve"},
        )

    async def assertion(adapter: RetrievalHttpClient, _captured: list[httpx.Request]) -> None:
        with pytest.raises(RetrievalGatewayError) as raised:
            await adapter.retrieve(_request())
        assert raised.value.code == "retrieval_service_error"
        assert calls == 1

    await _run_with_transport(handler, assertion)


def test_retrieval_rejects_unsafe_configuration() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    with pytest.raises(RetrievalGatewayError) as raised:
        RetrievalHttpClient(
            "http://user:secret@retrieval.test?redirect=untrusted",
            api_key="retrieval-secret",
            transport=transport,
        )
    assert raised.value.code == "retrieval_configuration_error"

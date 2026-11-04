"""HTTP adapter 的 fail-closed 與敏感資料邊界測試。"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from chat_service.application.dtos import AnswerGenerationRequest, RetrievalRequest
from chat_service.domain.entities import (
    AllowedEvidence,
    Citation,
    Governance,
    Principal,
    RestrictedEvidence,
)
from chat_service.domain.enums import Confidentiality
from chat_service.infrastructure.answer_generation.http_client import (
    AnswerGenerationGatewayError,
    AnswerGenerationHttpClient,
)
from chat_service.infrastructure.answer_generation.schemas import AnswerRequestGovernanceSchema
from chat_service.infrastructure.retrieval.http_client import (
    RetrievalGatewayError,
    RetrievalHttpClient,
)


def _restricted_evidence() -> RestrictedEvidence:
    chunk_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()
    return RestrictedEvidence(
        rank=1,
        chunk_id=chunk_id,
        document_id=document_id,
        version_id=version_id,
        citation=Citation(
            citation_id="R1",
            chunk_id=chunk_id,
            document_id=document_id,
            version_id=version_id,
            title="Restricted title",
            section_path="Restricted section",
            page_number=1,
        ),
    )


def _answer_request_with_restricted() -> AnswerGenerationRequest:
    # Domain DTO 目前以 tuple annotation 表達意圖，但 Python dataclass 不會在 runtime
    # 自動檢查 element type；adapter 的 concrete isinstance guard 必須在此邊界攔下它。
    return AnswerGenerationRequest(
        question="question",
        language="zh-TW",
        evidence=(_restricted_evidence(),),  # type: ignore[arg-type]
        context_attestation="opaque-attestation",
        request_id="request-123",
    )


@pytest.mark.asyncio
async def test_restricted_evidence_is_rejected_before_answer_http_call() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            500,
            json={
                "error": "unexpected",
                "message": "unexpected",
                "request_id": "downstream-request",
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    adapter = AnswerGenerationHttpClient(
        "http://answer.test",
        api_key="answer-secret",
        client=client,
    )
    try:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(_answer_request_with_restricted())
        assert raised.value.code == "answer_contract_error"
        assert calls == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_answer_error_body_and_secret_never_enter_exception_message() -> None:
    secret = "DOWNSTREAM-SECRET-CONTENT"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "error": "internal_error",
                "message": secret,
                "request_id": "downstream-request",
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    adapter = AnswerGenerationHttpClient(
        "http://answer.test",
        api_key="answer-secret",
        client=client,
    )
    try:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(
                AnswerGenerationRequest(
                    question="question",
                    language="zh-TW",
                    evidence=(
                        # Use a tiny valid allowed object without importing a test helper from
                        # the contract package.
                        _allowed_evidence_for_security(),
                    ),
                    context_attestation="opaque-attestation",
                    request_id="request-123",
                )
            )
        assert raised.value.code == "answer_service_error"
        assert secret not in str(raised.value)
        assert "answer-secret" not in str(raised.value)
    finally:
        await client.aclose()


def _allowed_evidence_for_security() -> AllowedEvidence:
    chunk_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()
    return AllowedEvidence(
        rank=1,
        chunk_id=chunk_id,
        document_id=document_id,
        version_id=version_id,
        score=0.8,
        content="safe content",
        document_type="policy",
        token_count=2,
        citation=Citation(
            citation_id="R1",
            chunk_id=chunk_id,
            document_id=document_id,
            version_id=version_id,
            title="Policy",
        ),
        governance=Governance(
            confidentiality=Confidentiality.INTERNAL,
            language="zh-TW",
            external_llm_allowed=True,
        ),
    )


def test_both_adapters_reject_blank_or_credentialed_configuration() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    with pytest.raises(RetrievalGatewayError) as retrieval_error:
        RetrievalHttpClient("http://user:secret@retrieval.test", api_key=" ", transport=transport)
    assert retrieval_error.value.code == "retrieval_configuration_error"
    with pytest.raises(AnswerGenerationGatewayError) as answer_error:
        AnswerGenerationHttpClient(
            "http://user:secret@answer.test?x=1", api_key=" ", transport=transport
        )
    assert answer_error.value.code == "answer_configuration_error"


def test_both_adapters_reject_non_printable_api_keys() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    with pytest.raises(RetrievalGatewayError) as retrieval_error:
        RetrievalHttpClient("http://retrieval.test", api_key=chr(0x91), transport=transport)
    assert retrieval_error.value.code == "retrieval_configuration_error"
    with pytest.raises(AnswerGenerationGatewayError) as answer_error:
        AnswerGenerationHttpClient("http://answer.test", api_key=chr(0x91), transport=transport)
    assert answer_error.value.code == "answer_configuration_error"


@pytest.mark.asyncio
async def test_request_id_and_principal_headers_reject_non_printable_values() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("the adapter must reject the header before making HTTP")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    retrieval = RetrievalHttpClient(
        "http://retrieval.test", api_key="retrieval-secret", client=client
    )
    answer = AnswerGenerationHttpClient(
        "http://answer.test", api_key="answer-secret", client=client
    )
    try:
        with pytest.raises(RetrievalGatewayError) as retrieval_error:
            await retrieval.retrieve(
                RetrievalRequest(
                    query="question",
                    limit=8,
                    language="zh-TW",
                    principal=Principal(
                        issuer="issuer",
                        subject=chr(0x80),
                        group_ids=("group",),
                    ),
                    request_id="request-123",
                )
            )
        assert retrieval_error.value.code == "retrieval_contract_error"

        with pytest.raises(AnswerGenerationGatewayError) as answer_error:
            await answer.generate(
                AnswerGenerationRequest(
                    question="question",
                    language="zh-TW",
                    evidence=(_allowed_evidence_for_security(),),
                    context_attestation="opaque-attestation",
                    request_id=chr(0x80),
                )
            )
        assert answer_error.value.code == "answer_contract_error"
        assert calls == 0
    finally:
        await client.aclose()


def test_answer_external_policy_flag_requires_a_real_json_boolean_true() -> None:
    with pytest.raises(ValidationError):
        AnswerRequestGovernanceSchema(
            confidentiality=Confidentiality.INTERNAL,
            language="zh-TW",
            external_llm_allowed=1,
        )


@pytest.mark.asyncio
async def test_answer_redirect_is_not_followed_and_is_not_retried() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "http://untrusted.test/answers"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    adapter = AnswerGenerationHttpClient(
        "http://answer.test", api_key="answer-secret", client=client
    )
    try:
        with pytest.raises(AnswerGenerationGatewayError) as raised:
            await adapter.generate(
                AnswerGenerationRequest(
                    question="question",
                    language="zh-TW",
                    evidence=(_allowed_evidence_for_security(),),
                    context_attestation="opaque-attestation",
                    request_id="request-123",
                )
            )
        assert raised.value.code == "answer_service_error"
        assert calls == 1
    finally:
        await client.aclose()

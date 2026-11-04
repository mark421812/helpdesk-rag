from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import yaml

from answer_generation_service.application.dtos import (
    GenerationPolicy,
    OutboundPolicyDecision,
    PromptMessage,
    ProviderGenerationRequest,
    ProviderGenerationResult,
)
from answer_generation_service.application.prompt_builder import GroundedPromptBuilder
from answer_generation_service.application.use_cases import GenerateGroundedAnswer
from answer_generation_service.bootstrap import Runtime
from answer_generation_service.domain.exceptions import (
    ProviderContractError,
    UnauthorizedContextError,
)
from answer_generation_service.infrastructure.config import DEFAULT_POLICY_PATH, Settings
from answer_generation_service.infrastructure.llm.fake import FakeLLMProvider
from answer_generation_service.infrastructure.outbound_policy.rule_based_policy import (
    RuleBasedOutboundPolicy,
)
from answer_generation_service.presentation.app import create_app
from tests.factories import request_body


class AcceptVerifier:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls = 0

    def verify(self, question: object, context: object, token: str) -> None:
        self.calls += 1
        if self.reject:
            raise UnauthorizedContextError("tampered secret context")

    def is_ready(self) -> bool:
        return True


class AllowPolicy:
    def evaluate(self, question: object, context: object) -> OutboundPolicyDecision:
        return OutboundPolicyDecision(allowed=True, policy_version="outbound-policy-v1")

    def is_ready(self) -> bool:
        return True


class FixedCounter:
    def count_messages(self, messages: tuple[PromptMessage, ...]) -> int:
        return 100


class ContractErrorProvider(FakeLLMProvider):
    async def generate(self, request: ProviderGenerationRequest) -> ProviderGenerationResult:
        raise ProviderContractError("synthetic blank provider response")


@pytest.fixture
def enabled_policy_path(tmp_path: Path) -> str:
    payload = yaml.safe_load(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    payload["enabled"] = True
    path = tmp_path / "enabled-policy.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return str(path)


def _app(
    *,
    verifier: AcceptVerifier | None = None,
    outbound_policy: object | None = None,
    provider: FakeLLMProvider | None = None,
    max_body_bytes: int = 262_144,
) -> tuple[object, FakeLLMProvider]:
    resolved_provider = provider or FakeLLMProvider()
    resolved_verifier = verifier or AcceptVerifier()
    resolved_policy = outbound_policy or AllowPolicy()
    use_case = GenerateGroundedAnswer(
        attestation_verifier=resolved_verifier,
        outbound_policy=resolved_policy,  # type: ignore[arg-type]
        token_counter=FixedCounter(),
        prompt_builder=GroundedPromptBuilder(),
        provider=resolved_provider,
        generation_policy=GenerationPolicy(
            max_context_tokens=3_000,
            max_prompt_tokens=12_000,
            max_output_tokens=1_024,
            max_answer_characters=20_000,
            temperature=0,
            prompt_version="grounded-answer-v1",
        ),
    )
    runtime = Runtime(
        provider=resolved_provider,
        attestation_verifier=resolved_verifier,
        outbound_policy=resolved_policy,  # type: ignore[arg-type]
        generate_answer=use_case,
    )
    settings = Settings(
        _env_file=None,
        api_internal_key="answer-key",
        api_max_request_body_bytes=max_body_bytes,
    )
    return create_app(settings=settings, runtime=runtime), resolved_provider


@pytest.mark.asyncio
async def test_valid_context_returns_only_safe_server_owned_citation() -> None:
    application, provider = _app()
    transport = httpx.ASGITransport(app=application)  # type: ignore[arg-type]

    async with application.router.lifespan_context(application):  # type: ignore[attr-defined]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/answers",
                json=request_body(),
                headers={"x-internal-api-key": "answer-key"},
            )

    assert response.status_code == 200
    assert response.json()["citations"][0]["title"] == "遠端工作辦法"
    assert "source_path" not in response.text
    assert "source_url" not in response.text
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_restricted_or_unknown_request_fields_never_reach_provider() -> None:
    application, provider = _app()
    transport = httpx.ASGITransport(app=application)  # type: ignore[arg-type]
    restricted = request_body()
    restricted["evidence"][0]["governance"]["external_llm_allowed"] = False  # type: ignore[index]
    with_extra = request_body()
    with_extra["restricted_evidence"] = []

    async with application.router.lifespan_context(application):  # type: ignore[attr-defined]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            restricted_response = await client.post(
                "/api/v1/answers",
                json=restricted,
                headers={"x-internal-api-key": "answer-key"},
            )
            extra_response = await client.post(
                "/api/v1/answers",
                json=with_extra,
                headers={"x-internal-api-key": "answer-key"},
            )

    assert restricted_response.status_code == 422
    assert extra_response.status_code == 422
    assert provider.requests == []


@pytest.mark.asyncio
async def test_invalid_service_key_and_attestation_never_call_provider() -> None:
    verifier = AcceptVerifier(reject=True)
    application, provider = _app(verifier=verifier)
    transport = httpx.ASGITransport(app=application)  # type: ignore[arg-type]

    async with application.router.lifespan_context(application):  # type: ignore[attr-defined]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthorized = await client.post(
                "/api/v1/answers",
                json=request_body(),
                headers={"x-internal-api-key": "wrong"},
            )
            invalid_context = await client.post(
                "/api/v1/answers",
                json=request_body(),
                headers={"x-internal-api-key": "answer-key"},
            )

    assert unauthorized.status_code == 401
    assert invalid_context.status_code == 403
    assert "tampered secret context" not in invalid_context.text
    assert provider.requests == []


@pytest.mark.asyncio
async def test_sensitive_question_is_blocked_before_fake_provider(
    enabled_policy_path: str,
) -> None:
    policy = RuleBasedOutboundPolicy(
        policy_path=enabled_policy_path,
        external_llm_approved=True,
    )
    application, provider = _app(outbound_policy=policy)
    transport = httpx.ASGITransport(app=application)  # type: ignore[arg-type]
    secret_value = "synthetic-secret-that-must-not-leak"

    async with application.router.lifespan_context(application):  # type: ignore[attr-defined]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/answers",
                json=request_body(question_text=f"password={secret_value}"),
                headers={"x-internal-api-key": "answer-key"},
            )

    assert response.status_code == 403
    assert response.json()["error"] == "outbound_policy_blocked"
    assert secret_value not in response.text
    assert provider.requests == []


@pytest.mark.asyncio
async def test_provider_contract_failure_maps_to_bad_gateway() -> None:
    application, _ = _app(provider=ContractErrorProvider())
    transport = httpx.ASGITransport(app=application)  # type: ignore[arg-type]

    async with application.router.lifespan_context(application):  # type: ignore[attr-defined]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/answers",
                json=request_body(),
                headers={"x-internal-api-key": "answer-key"},
            )

    assert response.status_code == 502
    assert response.json()["error"] == "provider_contract_error"


@pytest.mark.asyncio
async def test_readiness_checks_provider_attestation_and_policy() -> None:
    application, _ = _app()
    transport = httpx.ASGITransport(app=application)  # type: ignore[arg-type]

    async with application.router.lifespan_context(application):  # type: ignore[attr-defined]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == {
        "provider_configured": True,
        "attestation_keys": True,
        "outbound_policy": True,
    }


@pytest.mark.asyncio
async def test_chunked_oversized_body_is_rejected_before_verification() -> None:
    verifier = AcceptVerifier()
    application, provider = _app(verifier=verifier, max_body_bytes=16)
    transport = httpx.ASGITransport(app=application)  # type: ignore[arg-type]

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"question":"'
        yield b"this request is intentionally too large"

    async with application.router.lifespan_context(application):  # type: ignore[attr-defined]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/answers",
                content=chunks(),
                headers={
                    "content-type": "application/json",
                    "x-internal-api-key": "answer-key",
                    "x-request-id": "bounded-answer-body",
                },
            )

    assert response.status_code == 413
    assert response.headers["x-request-id"] == "bounded-answer-body"
    assert verifier.calls == 0
    assert provider.requests == []

from __future__ import annotations

import json

import httpx
import pytest

from answer_generation_service.application.dtos import PromptMessage, ProviderGenerationRequest
from answer_generation_service.domain.exceptions import (
    ProviderContractError,
    ProviderUnavailableError,
)
from answer_generation_service.infrastructure.llm.groq_hosted import GroqHostedProvider


def _request() -> ProviderGenerationRequest:
    return ProviderGenerationRequest(
        messages=(
            PromptMessage(role="system", content="Return the required JSON object."),
            PromptMessage(role="user", content="Synthetic public evidence only."),
        ),
        max_output_tokens=128,
        temperature=0,
    )


def _success() -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {"content": '{"status":"unable_to_answer","answer_parts":[]}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _provider(handler: httpx.MockTransport, **overrides: object) -> GroqHostedProvider:
    defaults: dict[str, object] = {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "gsk_synthetic_not_a_real_key",
        "model": "openai/gpt-oss-20b",
        "connect_timeout_seconds": 1.0,
        "read_timeout_seconds": 1.0,
        "total_timeout_seconds": 5.0,
        "max_response_bytes": 10_000,
        "total_attempts": 2,
        "retry_backoff_seconds": 0.0,
        "retry_max_backoff_seconds": 0.0,
        "max_concurrency": 2,
        "max_queue_size": 8,
        "queue_wait_timeout_seconds": 1.0,
        "transport": handler,
    }
    defaults.update(overrides)
    return GroqHostedProvider(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_uses_groq_chat_contract_and_json_object_mode() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer gsk_synthetic_not_a_real_key"
        payload = json.loads(request.content)
        assert payload["model"] == "openai/gpt-oss-20b"
        assert payload["max_completion_tokens"] == 128
        assert "max_tokens" not in payload
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["stream"] is False
        assert "tools" not in payload
        return httpx.Response(200, json=_success())

    provider = _provider(httpx.MockTransport(handler))
    result = await provider.generate(_request())
    await provider.close()

    assert result.provider == "groq"
    assert result.model == "openai/gpt-oss-20b"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 498, 500, 502, 503, 504])
async def test_transient_status_is_retried_once(status: int) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status, headers={"retry-after": "0"})
        return httpx.Response(200, json=_success())

    provider = _provider(httpx.MockTransport(handler))
    result = await provider.generate(_request())
    await provider.close()

    assert result.finish_reason == "stop"
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_auth_or_model_error_is_safe_and_not_retried(status: int) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="reflected-sensitive-prompt")

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailableError) as captured:
        await provider.generate(_request())
    await provider.close()

    assert calls == 1
    assert "reflected-sensitive-prompt" not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 413, 422])
async def test_invalid_request_status_is_a_contract_error_without_retry(status: int) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="do-not-expose-this-body")

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderContractError) as captured:
        await provider.generate(_request())
    await provider.close()

    assert calls == 1
    assert "do-not-expose-this-body" not in str(captured.value)


@pytest.mark.asyncio
async def test_groq_does_not_accept_or_poll_pending_responses() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(202, json={"requestId": "not-a-groq-contract"})

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderContractError, match="pending"):
        await provider.generate(_request())
    await provider.close()

    assert paths == ["/openai/v1/chat/completions"]


@pytest.mark.asyncio
async def test_unexpected_content_type_is_rejected() -> None:
    provider = _provider(
        httpx.MockTransport(lambda request: httpx.Response(200, text=json.dumps(_success())))
    )

    with pytest.raises(ProviderContractError, match="content type"):
        await provider.generate(_request())
    await provider.close()


@pytest.mark.asyncio
async def test_declared_oversized_response_is_rejected() -> None:
    provider = _provider(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json", "content-length": "10001"},
                content=b"{}",
            )
        ),
        max_response_bytes=10_000,
    )

    with pytest.raises(ProviderContractError, match="size limit"):
        await provider.generate(_request())
    await provider.close()

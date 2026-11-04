from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from answer_generation_service.application.dtos import PromptMessage, ProviderGenerationRequest
from answer_generation_service.domain.exceptions import (
    ProviderContractError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from answer_generation_service.infrastructure.llm.nvidia_hosted import NvidiaHostedNimProvider


def _request() -> ProviderGenerationRequest:
    return ProviderGenerationRequest(
        messages=(
            PromptMessage(role="system", content="fixed system policy"),
            PromptMessage(role="user", content="synthetic question and evidence"),
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


def _provider(handler: httpx.MockTransport, **overrides: object) -> NvidiaHostedNimProvider:
    defaults: dict[str, object] = {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key": "nvapi-synthetic",
        "model": "test/model",
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
    return NvidiaHostedNimProvider(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_maps_openai_compatible_response_and_owns_auth_header() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer nvapi-synthetic"
        payload = json.loads(request.content)
        assert payload["model"] == "test/model"
        assert payload["stream"] is False
        assert "tools" not in payload
        return httpx.Response(200, json=_success())

    provider = _provider(httpx.MockTransport(handler))
    result = await provider.generate(_request())
    await provider.close()

    assert result.provider == "nvidia_hosted_nim"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 5


@pytest.mark.asyncio
async def test_429_is_retried_within_bounded_attempts() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="do not log this reflected prompt")
        return httpx.Response(200, json=_success())

    provider = _provider(httpx.MockTransport(handler))
    result = await provider.generate(_request())
    await provider.close()

    assert result.finish_reason == "stop"
    assert calls == 2


@pytest.mark.asyncio
async def test_authentication_error_is_not_retried() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="secret reflected body")

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailableError):
        await provider.generate(_request())
    await provider.close()

    assert calls == 1


@pytest.mark.asyncio
async def test_202_pending_response_is_polled() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(202, json={"requestId": "pending-1"})
        return httpx.Response(200, json=_success())

    provider = _provider(httpx.MockTransport(handler))
    result = await provider.generate(_request())
    await provider.close()

    assert result.finish_reason == "stop"
    assert paths == ["/v1/chat/completions", "/v1/status/pending-1"]


@pytest.mark.asyncio
async def test_oversized_success_response_is_rejected() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"{" + b"x" * 500 + b"}",
        )
    )
    provider = _provider(transport, max_response_bytes=100)

    with pytest.raises(ProviderContractError, match="size limit"):
        await provider.generate(_request())
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [("", "stop"), ('{"status":"unable_to_answer","answer_parts":[]}', "")],
)
async def test_blank_provider_fields_are_contract_errors(
    content: str,
    finish_reason: str,
) -> None:
    response = _success()
    choice = response["choices"][0]  # type: ignore[index]
    choice["message"]["content"] = content  # type: ignore[index]
    choice["finish_reason"] = finish_reason  # type: ignore[index]
    provider = _provider(httpx.MockTransport(lambda request: httpx.Response(200, json=response)))

    with pytest.raises(ProviderContractError, match="blank"):
        await provider.generate(_request())
    await provider.close()


@pytest.mark.asyncio
async def test_non_finite_retry_after_does_not_escape_as_overflow() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "1e309"})
        return httpx.Response(200, json=_success())

    provider = _provider(httpx.MockTransport(handler))
    result = await provider.generate(_request())
    await provider.close()

    assert result.finish_reason == "stop"
    assert calls == 2


@pytest.mark.asyncio
async def test_poll_rate_limit_performs_only_one_backoff_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []
    backoffs: list[tuple[int, int | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(202, json={"requestId": "pending-rate-limit"})
        if paths.count("/v1/status/pending-rate-limit") == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=_success())

    async def record_backoff(
        attempt: int,
        retry_after_seconds: int | None,
        deadline: float,
    ) -> None:
        backoffs.append((attempt, retry_after_seconds))

    provider = _provider(httpx.MockTransport(handler))
    monkeypatch.setattr(provider, "_backoff", record_backoff)
    result = await provider.generate(_request())
    await provider.close()

    assert result.finish_reason == "stop"
    assert backoffs == [(0, None), (1, 0)]


@pytest.mark.asyncio
async def test_thirty_concurrent_requests_have_bounded_active_and_queued_capacity() -> None:
    release_provider = asyncio.Event()
    active_calls = 0
    maximum_active_calls = 0
    two_calls_are_active = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_calls, maximum_active_calls
        active_calls += 1
        maximum_active_calls = max(maximum_active_calls, active_calls)
        if active_calls == 2:
            two_calls_are_active.set()
        try:
            await release_provider.wait()
            return httpx.Response(200, json=_success())
        finally:
            active_calls -= 1

    # 最多 2 個 active + 3 個 queued；其餘 25 個必須立即被 backpressure 拒絕。
    provider = _provider(
        httpx.MockTransport(handler),
        max_concurrency=2,
        max_queue_size=3,
        queue_wait_timeout_seconds=1.0,
    )
    tasks = [asyncio.create_task(provider.generate(_request())) for _ in range(30)]
    await asyncio.wait_for(two_calls_are_active.wait(), timeout=1.0)
    await asyncio.sleep(0.05)

    assert sum(task.done() for task in tasks) == 25
    release_provider.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    await provider.close()

    assert maximum_active_calls == 2
    assert sum(isinstance(result, ProviderRateLimitError) for result in results) == 25
    assert sum(not isinstance(result, BaseException) for result in results) == 5


@pytest.mark.asyncio
async def test_queue_wait_timeout_is_rate_limit_and_is_capped_by_total_deadline() -> None:
    provider = _provider(
        httpx.MockTransport(lambda request: httpx.Response(200, json=_success())),
        max_concurrency=1,
        max_queue_size=1,
        queue_wait_timeout_seconds=5.0,
        total_timeout_seconds=0.05,
    )
    # 直接占用唯一 slot，讓下一個 request 只測 admission queue；queue timeout 設為
    # 5 秒，但 total deadline 為 0.05 秒，因此等待必須由較短的整體期限截止。
    await provider._semaphore.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(ProviderRateLimitError):
            await provider.generate(_request())
    finally:
        provider._semaphore.release()
        await provider.close()

    assert time.monotonic() - started < 0.5

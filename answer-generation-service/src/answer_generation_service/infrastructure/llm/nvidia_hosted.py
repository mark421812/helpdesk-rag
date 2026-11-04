"""NVIDIA hosted NIM 的 OpenAI-compatible adapter。"""

from __future__ import annotations

import httpx

from answer_generation_service.infrastructure.llm.openai_compatible import (
    OpenAICompatibleChatProvider,
)


class NvidiaHostedNimProvider(OpenAICompatibleChatProvider):
    """保留 NVIDIA 的舊介面，供 composition root 與既有 contract tests 使用。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        total_timeout_seconds: float,
        max_response_bytes: int,
        total_attempts: int,
        retry_backoff_seconds: float,
        retry_max_backoff_seconds: float,
        max_concurrency: int,
        max_queue_size: int,
        queue_wait_timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            provider_name="nvidia_hosted_nim",
            base_url=base_url,
            api_key=api_key,
            model=model,
            token_limit_field="max_tokens",
            response_format=None,
            supports_pending_responses=True,
            retryable_statuses=frozenset({500, 502, 503, 504}),
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            total_timeout_seconds=total_timeout_seconds,
            max_response_bytes=max_response_bytes,
            total_attempts=total_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            retry_max_backoff_seconds=retry_max_backoff_seconds,
            max_concurrency=max_concurrency,
            max_queue_size=max_queue_size,
            queue_wait_timeout_seconds=queue_wait_timeout_seconds,
            transport=transport,
            client=client,
        )

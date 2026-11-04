"""完全離線、固定輸出的 Fake Provider，供 unit tests 與 Docker smoke 使用。"""

from __future__ import annotations

import json

from answer_generation_service.application.dtos import (
    ProviderGenerationRequest,
    ProviderGenerationResult,
)


class FakeLLMProvider:
    def __init__(self, *, response_content: str | None = None) -> None:
        self._response_content = response_content or json.dumps(
            {
                "status": "answered",
                "answer_parts": [
                    {
                        "text": "根據提供的公司文件，已找到與問題相關的規定。",
                        "citation_ids": ["C1"],
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.requests: list[ProviderGenerationRequest] = []

    async def generate(self, request: ProviderGenerationRequest) -> ProviderGenerationResult:
        self.requests.append(request)
        return ProviderGenerationResult(
            content=self._response_content,
            provider="fake",
            model="fake-grounded-v1",
            finish_reason="stop",
        )

    def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

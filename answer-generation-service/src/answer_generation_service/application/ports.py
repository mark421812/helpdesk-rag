"""Application 依賴的抽象 ports；Infrastructure adapters 在 Composition Root 注入。"""

from __future__ import annotations

from typing import Protocol

from answer_generation_service.application.dtos import (
    OutboundPolicyDecision,
    PromptMessage,
    ProviderGenerationRequest,
    ProviderGenerationResult,
)
from answer_generation_service.domain.entities import ApprovedContext, Question


class LLMProviderPort(Protocol):
    async def generate(self, request: ProviderGenerationRequest) -> ProviderGenerationResult: ...

    def is_ready(self) -> bool: ...

    async def close(self) -> None: ...


class ContextAttestationVerifierPort(Protocol):
    def verify(
        self,
        question: Question,
        context: ApprovedContext,
        token: str,
    ) -> None: ...

    def is_ready(self) -> bool: ...


class OutboundPolicyPort(Protocol):
    def evaluate(
        self,
        question: Question,
        context: ApprovedContext,
    ) -> OutboundPolicyDecision: ...

    def is_ready(self) -> bool: ...


class GenerationTokenCounterPort(Protocol):
    def count_messages(self, messages: tuple[PromptMessage, ...]) -> int: ...

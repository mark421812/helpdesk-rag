"""Application layer 的 framework-free DTO。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from answer_generation_service.domain.entities import ApprovedContext, GroundedAnswer, Question
from answer_generation_service.domain.exceptions import DomainValidationError


@dataclass(frozen=True, slots=True)
class GenerateAnswerCommand:
    question: Question
    context: ApprovedContext
    context_attestation: str

    def __post_init__(self) -> None:
        if not self.context_attestation.strip():
            raise DomainValidationError("context_attestation must not be blank")


@dataclass(frozen=True, slots=True)
class PromptMessage:
    role: Literal["system", "user"]
    content: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise DomainValidationError("prompt message content must not be blank")


@dataclass(frozen=True, slots=True)
class ProviderGenerationRequest:
    messages: tuple[PromptMessage, ...]
    max_output_tokens: int
    temperature: float

    def __post_init__(self) -> None:
        if not self.messages or self.messages[0].role != "system":
            raise DomainValidationError("provider request must begin with a system message")
        if self.max_output_tokens <= 0:
            raise DomainValidationError("max_output_tokens must be positive")
        if not 0 <= self.temperature <= 2:
            raise DomainValidationError("temperature must be between 0 and 2")


@dataclass(frozen=True, slots=True)
class ProviderGenerationResult:
    content: str
    provider: str
    model: str
    finish_reason: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("provider content", self.content),
            ("provider", self.provider),
            ("model", self.model),
            ("finish_reason", self.finish_reason),
        ):
            if not value.strip():
                raise DomainValidationError(f"{field_name} must not be blank")
        for token_value in (self.prompt_tokens, self.completion_tokens):
            if token_value is not None and token_value < 0:
                raise DomainValidationError("provider token usage must not be negative")


@dataclass(frozen=True, slots=True)
class OutboundPolicyDecision:
    allowed: bool
    policy_version: str
    categories: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise DomainValidationError("policy_version must not be blank")
        if self.allowed and (self.categories or self.rule_ids):
            raise DomainValidationError("allowed policy decision must not contain violations")


@dataclass(frozen=True, slots=True)
class GenerationPolicy:
    max_context_tokens: int
    max_prompt_tokens: int
    max_output_tokens: int
    max_answer_characters: int
    temperature: float
    prompt_version: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("max_context_tokens", self.max_context_tokens),
            ("max_prompt_tokens", self.max_prompt_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("max_answer_characters", self.max_answer_characters),
        ):
            if value <= 0:
                raise DomainValidationError(f"{field_name} must be positive")
        if not 0 <= self.temperature <= 2:
            raise DomainValidationError("temperature must be between 0 and 2")
        if not self.prompt_version.strip():
            raise DomainValidationError("prompt_version must not be blank")


@dataclass(frozen=True, slots=True)
class GeneratedCitation:
    citation_id: str
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    title: str
    section_path: str | None
    page_number: int | None


@dataclass(frozen=True, slots=True)
class GenerationDiagnostics:
    provider: str
    model: str
    prompt_version: str
    finish_reason: str
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True, slots=True)
class GenerateAnswerResult:
    answer: GroundedAnswer
    citations: tuple[GeneratedCitation, ...]
    diagnostics: GenerationDiagnostics

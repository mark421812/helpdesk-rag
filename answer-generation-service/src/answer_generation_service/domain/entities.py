"""Grounded answer 的 Domain entities 與不可破壞的 invariants。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from answer_generation_service.domain.enums import AnswerStatus, Confidentiality
from answer_generation_service.domain.exceptions import DomainValidationError


def _require_text(field_name: str, value: str) -> None:
    if not value.strip():
        raise DomainValidationError(f"{field_name} must not be blank")


def _optional_text(field_name: str, value: str | None) -> None:
    if value is not None:
        _require_text(field_name, value)


@dataclass(frozen=True, slots=True)
class Question:
    text: str
    language: str | None = None

    def __post_init__(self) -> None:
        _require_text("question", self.text)
        _optional_text("question language", self.language)


@dataclass(frozen=True, slots=True)
class SafeCitation:
    """型別刻意沒有 source_path/source_url，避免誤洩漏實體位置。"""

    title: str
    section_path: str | None
    page_number: int | None

    def __post_init__(self) -> None:
        _require_text("citation title", self.title)
        _optional_text("citation section_path", self.section_path)
        if self.page_number is not None and self.page_number <= 0:
            raise DomainValidationError("citation page_number must be positive")


@dataclass(frozen=True, slots=True)
class Governance:
    confidentiality: Confidentiality
    language: str
    external_llm_allowed: bool

    def __post_init__(self) -> None:
        _require_text("governance language", self.language)
        if self.external_llm_allowed is not True:
            raise DomainValidationError("evidence is forbidden from external LLM processing")


@dataclass(frozen=True, slots=True)
class ApprovedEvidence:
    rank: int
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    content: str
    document_type: str
    token_count: int
    citation: SafeCitation
    governance: Governance

    def __post_init__(self) -> None:
        if self.rank <= 0:
            raise DomainValidationError("evidence rank must be positive")
        _require_text("evidence content", self.content)
        _require_text("evidence document_type", self.document_type)
        if self.token_count <= 0:
            raise DomainValidationError("evidence token_count must be positive")


@dataclass(frozen=True, slots=True)
class ApprovedContext:
    evidence: tuple[ApprovedEvidence, ...]

    def __post_init__(self) -> None:
        if not self.evidence:
            raise DomainValidationError("evidence must not be empty")
        ranks = [item.rank for item in self.evidence]
        if ranks != sorted(ranks) or len(set(ranks)) != len(ranks):
            raise DomainValidationError("evidence ranks must be unique and ascending")
        chunk_ids = [item.chunk_id for item in self.evidence]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise DomainValidationError("evidence chunk_id values must be unique")


@dataclass(frozen=True, slots=True)
class AnswerPart:
    text: str
    citation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text("answer part text", self.text)
        if not self.citation_ids:
            raise DomainValidationError("answered part must cite at least one evidence item")
        if any(not citation_id.strip() for citation_id in self.citation_ids):
            raise DomainValidationError("citation_ids must not contain blank values")
        if len(set(self.citation_ids)) != len(self.citation_ids):
            raise DomainValidationError("citation_ids must not contain duplicates")


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    status: AnswerStatus
    parts: tuple[AnswerPart, ...]

    def __post_init__(self) -> None:
        if self.status is AnswerStatus.ANSWERED and not self.parts:
            raise DomainValidationError("answered result must contain answer parts")
        if self.status is AnswerStatus.UNABLE_TO_ANSWER and self.parts:
            raise DomainValidationError("unable_to_answer result must not contain answer text")

"""Ingestion 可預期錯誤階層；錯誤型別也決定 retry、隔離或 HTTP status。"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected ingestion domain failures."""


class ValidationError(DomainError):
    """Raised when an entity or value object violates an invariant."""


class MetadataMissingError(ValidationError):
    """Raised when a document is missing metadata that must not be inferred."""

    def __init__(self, missing_fields: tuple[str, ...]) -> None:
        self.missing_fields = tuple(sorted(set(missing_fields)))
        super().__init__(f"Missing required metadata: {', '.join(self.missing_fields)}")


class ParserRequiresOcrError(DomainError):
    """Raised when a PDF has no extractable text and requires the future OCR flow."""


class UnsupportedDocumentError(DomainError):
    """Raised when no enabled parser can process a file."""


class FileTooLargeError(DomainError):
    """Raised when a file exceeds its source's configured size limit."""


class SourceChangedError(DomainError):
    """Raised when a source file no longer matches the discovered snapshot."""


class NotFoundError(DomainError):
    """Raised when an application aggregate cannot be found."""


class ConflictError(DomainError):
    """Raised when an operation conflicts with current persisted state."""


class LeaseLostError(ConflictError):
    """Raised when a worker no longer owns a job lease."""


class ExternalServiceError(DomainError):
    """An expected downstream failure with an explicit retry policy."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        self.retryable = retryable
        super().__init__(message)

"""Application layer 使用的資料傳輸物件（DTO）。

DTO 只攜帶 use case 所需資料，不包含 HTTP、JSON 或資料庫實作細節。這些 class 使用
``@dataclass(frozen=True, slots=True)``：dataclass 自動產生建構子；frozen 代表建立後不可修改；
slots 可防止誤加不存在的屬性並減少記憶體使用量。
"""

from __future__ import annotations

from dataclasses import dataclass

from retrieval_service.domain.entities import CandidateChunk, PrincipalContext
from retrieval_service.domain.enums import DocumentStatus
from retrieval_service.domain.exceptions import DomainValidationError


def _validate_optional_text(field_name: str, value: str | None) -> None:
    """若 optional 字串存在，就不允許它只有空白。

    ``str | None`` 是 Python 3.10+ 的 union type，表示值可以是字串或 ``None``。
    """

    if value is not None and not value.strip():
        raise DomainValidationError(f"{field_name} must not be blank")


@dataclass(frozen=True, slots=True)
class IdentityInput:
    """與 HTTP 無關的身分輸入，交由 identity adapter 驗證。"""

    bearer_token: str | None = None
    development_user_id: str | None = None
    development_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """dataclass 建構完成後自動執行的跨欄位驗證。"""

        _validate_optional_text("bearer_token", self.bearer_token)
        _validate_optional_text("development_user_id", self.development_user_id)
        if any(not group_id.strip() for group_id in self.development_group_ids):
            raise DomainValidationError("development_group_ids must not contain blank values")
        if len(set(self.development_group_ids)) != len(self.development_group_ids):
            raise DomainValidationError("development_group_ids must not contain duplicates")


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """集中保存候選數量、context 預算與 score threshold 等檢索政策。"""

    candidate_multiplier: int
    max_candidate_limit: int
    max_context_tokens: int
    max_chunks_per_document: int
    score_threshold: float | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("candidate_multiplier", self.candidate_multiplier),
            ("max_candidate_limit", self.max_candidate_limit),
            ("max_context_tokens", self.max_context_tokens),
            ("max_chunks_per_document", self.max_chunks_per_document),
        ):
            if value <= 0:
                raise DomainValidationError(f"{field_name} must be positive")
        if self.score_threshold is not None and not -1 <= self.score_threshold <= 1:
            raise DomainValidationError("score_threshold must be between -1 and 1")

    def candidate_limit_for(self, requested_limit: int) -> int:
        """多抓候選供後續 ACL/去重過濾，但永遠不超過安全上限。"""

        if requested_limit <= 0:
            raise DomainValidationError("requested_limit must be positive")
        if requested_limit > self.max_candidate_limit:
            raise DomainValidationError(
                "requested result limit must not exceed max_candidate_limit"
            )
        return min(self.max_candidate_limit, requested_limit * self.candidate_multiplier)


@dataclass(frozen=True, slots=True)
class KnowledgeSearchCriteria:
    """Application 送給 KnowledgeSearchPort 的查詢條件。"""

    query: str
    limit: int
    principal: PrincipalContext
    language: str | None
    score_threshold: float | None
    status: DocumentStatus = DocumentStatus.PUBLISHED

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise DomainValidationError("search query must not be blank")
        if self.limit <= 0:
            raise DomainValidationError("search limit must be positive")
        _validate_optional_text("search language", self.language)
        if self.score_threshold is not None and not -1 <= self.score_threshold <= 1:
            raise DomainValidationError("search score_threshold must be between -1 and 1")
        if self.status != DocumentStatus.PUBLISHED:
            raise DomainValidationError("employee retrieval may only search published documents")


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    """Knowledge Search adapter 回給 use case 的 framework-free 結果。"""

    candidates: tuple[CandidateChunk, ...]
    model_id: str
    model_revision: str

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise DomainValidationError("model_id must not be blank")
        if not self.model_revision.strip():
            raise DomainValidationError("model_revision must not be blank")

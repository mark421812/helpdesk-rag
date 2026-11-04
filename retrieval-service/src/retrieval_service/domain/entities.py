"""Retrieval 的 Domain entities 與 invariants。

Entity 不只是資料容器；``__post_init__`` 會保證物件一建立就處於合法狀態。這讓後面的
use case 不必到處重複檢查空字串、負數 token 或錯誤的治理分流。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from uuid import UUID

from retrieval_service.domain.enums import Confidentiality, RetrievalStatus
from retrieval_service.domain.exceptions import DomainValidationError


def _require_non_blank(field_name: str, value: str) -> None:
    """共用的非空白字串驗證。開頭底線表示模組私有 helper。"""

    if not value.strip():
        raise DomainValidationError(f"{field_name} must not be blank")


def _validate_optional_text(field_name: str, value: str | None) -> None:
    if value is not None:
        _require_non_blank(field_name, value)


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    """Identity adapter 驗證後的可信使用者；永遠不能直接由 JSON body 建立。"""

    user_id: str
    group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_blank("user_id", self.user_id)
        if any(not group_id.strip() for group_id in self.group_ids):
            raise DomainValidationError("group_ids must not contain blank values")
        if len(set(self.group_ids)) != len(self.group_ids):
            raise DomainValidationError("group_ids must not contain duplicates")


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """經 Presentation schema 初步驗證後，交給 use case 的查詢值物件。"""

    text: str
    limit: int
    language: str | None = None

    def __post_init__(self) -> None:
        _require_non_blank("query text", self.text)
        if self.limit <= 0:
            raise DomainValidationError("query limit must be positive")
        _validate_optional_text("language", self.language)


@dataclass(frozen=True, slots=True)
class CandidateChunk:
    """KIS 已完成 canonical ACL 檢查後回傳的候選 chunk。"""

    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    score: float
    content: str
    title: str
    document_type: str
    section_path: str | None
    page_number: int | None
    confidentiality: Confidentiality
    language: str
    external_llm_allowed: bool
    token_count: int
    chunk_index: int

    def __post_init__(self) -> None:
        # tuple unpacking 讓相同規則可以套到多個欄位，減少重複 if。
        if not isfinite(self.score):
            raise DomainValidationError("candidate score must be finite")
        for field_name, value in (
            ("content", self.content),
            ("title", self.title),
            ("document_type", self.document_type),
            ("language", self.language),
        ):
            _require_non_blank(field_name, value)
        _validate_optional_text("section_path", self.section_path)
        if self.page_number is not None and self.page_number <= 0:
            raise DomainValidationError("page_number must be positive")
        if self.token_count <= 0:
            raise DomainValidationError("token_count must be positive")
        if self.chunk_index < 0:
            raise DomainValidationError("chunk_index must not be negative")


@dataclass(frozen=True, slots=True)
class Citation:
    """可以安全回傳給 Client 的引用欄位。

    型別本身刻意沒有 filesystem path/source URL，因此 adapter 即使收到也無法誤塞進回應。
    """

    title: str
    section_path: str | None
    page_number: int | None

    def __post_init__(self) -> None:
        _require_non_blank("citation title", self.title)
        _validate_optional_text("citation section_path", self.section_path)
        if self.page_number is not None and self.page_number <= 0:
            raise DomainValidationError("citation page_number must be positive")


@dataclass(frozen=True, slots=True)
class Governance:
    """決定 evidence 可見性與是否允許外送的治理資訊。"""

    confidentiality: Confidentiality
    language: str
    external_llm_allowed: bool

    def __post_init__(self) -> None:
        _require_non_blank("governance language", self.language)


@dataclass(frozen=True, slots=True)
class Evidence:
    """經 Retrieval policy 選取、可以回傳給上層的證據。"""

    rank: int
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    score: float
    content: str
    document_type: str
    token_count: int
    citation: Citation
    governance: Governance

    def __post_init__(self) -> None:
        if self.rank <= 0:
            raise DomainValidationError("evidence rank must be positive")
        if not isfinite(self.score):
            raise DomainValidationError("evidence score must be finite")
        _require_non_blank("evidence content", self.content)
        _require_non_blank("evidence document_type", self.document_type)
        if self.token_count <= 0:
            raise DomainValidationError("evidence token_count must be positive")

    @classmethod
    def from_candidate(cls, candidate: CandidateChunk, *, rank: int) -> Evidence:
        """Named constructor：把候選轉成 Evidence 並只建立安全 Citation。

        ``cls`` 代表目前 class；``*`` 後的 rank 必須以具名參數傳入，避免呼叫時看不懂
        第二個整數代表什麼。
        """

        return cls(
            rank=rank,
            chunk_id=candidate.chunk_id,
            document_id=candidate.document_id,
            version_id=candidate.version_id,
            score=candidate.score,
            content=candidate.content,
            document_type=candidate.document_type,
            token_count=candidate.token_count,
            citation=Citation(
                title=candidate.title,
                section_path=candidate.section_path,
                page_number=candidate.page_number,
            ),
            governance=Governance(
                confidentiality=candidate.confidentiality,
                language=candidate.language,
                external_llm_allowed=candidate.external_llm_allowed,
            ),
        )


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """一次 Retrieval 的完整結果，並在建立時驗證治理分流 invariants。"""

    status: RetrievalStatus
    llm_context: tuple[Evidence, ...]
    restricted_evidence: tuple[Evidence, ...]
    candidate_count: int
    model_id: str
    model_revision: str

    def __post_init__(self) -> None:
        # ``any(...)`` 遇到第一個 True 就停止，適合驗證集合中是否混入違規 evidence。
        if self.candidate_count < 0:
            raise DomainValidationError("candidate_count must not be negative")
        _require_non_blank("model_id", self.model_id)
        _require_non_blank("model_revision", self.model_revision)

        if any(not item.governance.external_llm_allowed for item in self.llm_context):
            raise DomainValidationError(
                "llm_context contains evidence forbidden from external LLMs"
            )
        if any(item.governance.external_llm_allowed for item in self.restricted_evidence):
            raise DomainValidationError(
                "restricted_evidence contains evidence allowed for external LLMs"
            )
        for field_name, field_evidence in (
            ("llm_context", self.llm_context),
            ("restricted_evidence", self.restricted_evidence),
        ):
            field_ranks = [item.rank for item in field_evidence]
            if field_ranks != sorted(field_ranks):
                raise DomainValidationError(f"{field_name} must be ordered by ascending rank")

        evidence = self.all_evidence
        if self.candidate_count < len(evidence):
            raise DomainValidationError("candidate_count must cover all selected evidence")
        expected_ranks = list(range(1, len(evidence) + 1))
        if [item.rank for item in evidence] != expected_ranks:
            raise DomainValidationError("evidence ranks must be unique and contiguous")

        expected_status = RetrievalStatus.INSUFFICIENT_EVIDENCE
        if self.llm_context:
            expected_status = RetrievalStatus.EVIDENCE_FOUND
        elif self.restricted_evidence:
            expected_status = RetrievalStatus.EXTERNAL_GENERATION_BLOCKED
        if self.status != expected_status:
            raise DomainValidationError(
                f"retrieval status must be {expected_status.value} for the supplied evidence"
            )

    @property
    def all_evidence(self) -> tuple[Evidence, ...]:
        """把兩個治理分流合併後按全域 rank 排序。

        ``@property`` 讓呼叫端以 ``result.all_evidence`` 讀取，不需要加括號。
        """

        return tuple(
            sorted(
                self.llm_context + self.restricted_evidence,
                key=lambda evidence: evidence.rank,
            )
        )

    @property
    def selected_count(self) -> int:
        """已選取證據總數。"""

        return len(self.llm_context) + len(self.restricted_evidence)

    @property
    def external_generation_blocked(self) -> bool:
        """只有 restricted evidence 時，上層不得呼叫外部 LLM。"""

        return self.status == RetrievalStatus.EXTERNAL_GENERATION_BLOCKED

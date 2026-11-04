from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from retrieval_service.application.dtos import (
    KnowledgeSearchCriteria,
    KnowledgeSearchResult,
    RetrievalPolicy,
)
from retrieval_service.application.use_cases import RetrieveEvidence
from retrieval_service.domain.entities import CandidateChunk, PrincipalContext, RetrievalQuery
from retrieval_service.domain.enums import Confidentiality, DocumentStatus, RetrievalStatus
from retrieval_service.domain.exceptions import DomainValidationError


@dataclass
class FakeKnowledgeSearch:
    result: KnowledgeSearchResult
    requests: list[KnowledgeSearchCriteria] = field(default_factory=list)

    async def search(self, criteria: KnowledgeSearchCriteria) -> KnowledgeSearchResult:
        self.requests.append(criteria)
        return self.result

    async def is_ready(self) -> bool:
        return True


def _policy(
    *,
    score_threshold: float | None = None,
    max_context_tokens: int = 100,
    max_chunks_per_document: int = 2,
) -> RetrievalPolicy:
    return RetrievalPolicy(
        candidate_multiplier=3,
        max_candidate_limit=50,
        max_context_tokens=max_context_tokens,
        max_chunks_per_document=max_chunks_per_document,
        score_threshold=score_threshold,
    )


def _candidate(
    *,
    score: float,
    content: str,
    document_id: UUID | None = None,
    token_count: int = 10,
    external_llm_allowed: bool = True,
    chunk_index: int = 0,
) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=uuid4(),
        document_id=document_id or uuid4(),
        version_id=uuid4(),
        score=score,
        content=content,
        title=f"文件 {content}",
        document_type="policy",
        section_path="規章",
        page_number=1,
        confidentiality=Confidentiality.INTERNAL,
        language="zh-TW",
        external_llm_allowed=external_llm_allowed,
        token_count=token_count,
        chunk_index=chunk_index,
    )


def _result(*candidates: CandidateChunk) -> KnowledgeSearchResult:
    return KnowledgeSearchResult(
        candidates=tuple(candidates),
        model_id="embedding-model",
        model_revision="revision-1",
    )


@pytest.mark.asyncio
async def test_execute_forces_published_and_passes_the_trusted_principal() -> None:
    search = FakeKnowledgeSearch(result=_result())
    use_case = RetrieveEvidence(search, _policy())
    principal = PrincipalContext(user_id="employee-1", group_ids=("hr",))

    await use_case.execute(
        RetrievalQuery(text="遠端工作規定", limit=4, language="zh-TW"),
        principal,
    )

    request = search.requests[0]
    assert request.status is DocumentStatus.PUBLISHED
    assert request.principal is principal
    assert request.query == "遠端工作規定"
    assert request.language == "zh-TW"
    assert request.limit == 12


@pytest.mark.asyncio
async def test_selection_is_stable_and_applies_threshold_dedup_and_document_cap() -> None:
    capped_document_id = uuid4()
    first_tie = _candidate(
        score=0.9,
        content="相同分數先出現",
        document_id=capped_document_id,
        chunk_index=0,
    )
    second_tie = _candidate(score=0.9, content="相同分數後出現", chunk_index=0)
    duplicate = _candidate(score=0.85, content="相同分數先出現", chunk_index=1)
    over_document_cap = _candidate(
        score=0.8,
        content="同一文件第二段",
        document_id=capped_document_id,
        chunk_index=2,
    )
    below_threshold = _candidate(score=0.4, content="低分內容", chunk_index=0)
    search = FakeKnowledgeSearch(
        result=_result(
            below_threshold,
            duplicate,
            second_tie,
            over_document_cap,
            first_tie,
        )
    )
    use_case = RetrieveEvidence(
        search,
        _policy(score_threshold=0.5, max_chunks_per_document=1),
    )

    result = await use_case.execute(
        RetrievalQuery(text="公司規定", limit=5),
        PrincipalContext(user_id="employee-1"),
    )

    assert [item.content for item in result.all_evidence] == [
        "相同分數後出現",
        "相同分數先出現",
    ]
    assert [item.rank for item in result.all_evidence] == [1, 2]


@pytest.mark.asyncio
async def test_context_budget_skips_large_allowed_chunk_but_keeps_restricted_evidence() -> None:
    search = FakeKnowledgeSearch(
        result=_result(
            _candidate(score=0.95, content="過大的外部內容", token_count=51),
            _candidate(
                score=0.9,
                content="不可外送的原文",
                token_count=1_000,
                external_llm_allowed=False,
            ),
            _candidate(score=0.85, content="可外送的小段落", token_count=40),
            _candidate(score=0.8, content="超過剩餘額度", token_count=11),
        )
    )
    use_case = RetrieveEvidence(search, _policy(max_context_tokens=50))

    result = await use_case.execute(
        RetrievalQuery(text="公司規定", limit=4),
        PrincipalContext(user_id="employee-1"),
    )

    assert [item.content for item in result.llm_context] == ["可外送的小段落"]
    assert [item.content for item in result.restricted_evidence] == ["不可外送的原文"]
    assert result.status is RetrievalStatus.EVIDENCE_FOUND
    assert result.external_generation_blocked is False


@pytest.mark.asyncio
async def test_restricted_duplicate_cannot_hide_an_allowed_copy_of_the_same_content() -> None:
    shared_content = "這段規章也存在於允許外部處理的公開版本。"
    search = FakeKnowledgeSearch(
        result=_result(
            _candidate(
                score=0.95,
                content=shared_content,
                external_llm_allowed=False,
            ),
            _candidate(
                score=0.85,
                content=shared_content,
                external_llm_allowed=True,
            ),
        )
    )
    use_case = RetrieveEvidence(search, _policy())

    result = await use_case.execute(
        RetrievalQuery(text="公司規章", limit=2),
        PrincipalContext(user_id="employee-1"),
    )

    assert [item.content for item in result.llm_context] == [shared_content]
    assert [item.content for item in result.restricted_evidence] == [shared_content]
    assert result.status is RetrievalStatus.EVIDENCE_FOUND


@pytest.mark.asyncio
async def test_only_restricted_evidence_blocks_external_generation() -> None:
    search = FakeKnowledgeSearch(
        result=_result(
            _candidate(
                score=0.9,
                content="機密但使用者有權查看",
                external_llm_allowed=False,
            )
        )
    )
    use_case = RetrieveEvidence(search, _policy())

    result = await use_case.execute(
        RetrievalQuery(text="機密規章", limit=3),
        PrincipalContext(user_id="employee-1"),
    )

    assert result.status is RetrievalStatus.EXTERNAL_GENERATION_BLOCKED
    assert result.external_generation_blocked is True
    assert result.llm_context == ()
    assert len(result.restricted_evidence) == 1


@pytest.mark.asyncio
async def test_no_candidates_returns_insufficient_evidence() -> None:
    search = FakeKnowledgeSearch(result=_result())
    use_case = RetrieveEvidence(search, _policy())

    result = await use_case.execute(
        RetrievalQuery(text="找不到的問題", limit=3),
        PrincipalContext(user_id="employee-1"),
    )

    assert result.status is RetrievalStatus.INSUFFICIENT_EVIDENCE
    assert result.selected_count == 0
    assert result.all_evidence == ()


@pytest.mark.asyncio
async def test_result_limit_cannot_exceed_candidate_limit() -> None:
    use_case = RetrieveEvidence(
        FakeKnowledgeSearch(result=_result()),
        RetrievalPolicy(
            candidate_multiplier=3,
            max_candidate_limit=2,
            max_context_tokens=100,
            max_chunks_per_document=2,
        ),
    )

    with pytest.raises(DomainValidationError, match="max_candidate_limit"):
        await use_case.execute(
            RetrievalQuery(text="公司規定", limit=3),
            PrincipalContext(user_id="employee-1"),
        )

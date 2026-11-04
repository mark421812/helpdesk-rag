"""Retrieval 的核心 application use case。

這個模組只知道 ``KnowledgeSearchPort``，不知道搜尋實際是 HTTP、Qdrant 或資料庫。
這就是 Dependency Inversion：高階商業規則依賴介面，低階 adapter 實作介面。
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from retrieval_service.application.dtos import KnowledgeSearchCriteria, RetrievalPolicy
from retrieval_service.application.ports import KnowledgeSearchPort
from retrieval_service.domain.entities import (
    CandidateChunk,
    Evidence,
    PrincipalContext,
    RetrievalQuery,
    RetrievalResult,
)
from retrieval_service.domain.enums import RetrievalStatus


class RetrieveEvidence:
    """查詢候選、套用 evidence policy，再安全分流外部 LLM context。"""

    def __init__(self, knowledge_search: KnowledgeSearchPort, policy: RetrievalPolicy) -> None:
        self._knowledge_search = knowledge_search
        self._policy = policy

    async def execute(
        self,
        query: RetrievalQuery,
        principal: PrincipalContext,
    ) -> RetrievalResult:
        """執行一次完整檢索。

        ``async def`` 會回傳 coroutine；呼叫者必須用 ``await`` 等待 I/O 完成，但等待期間
        event loop 可以處理其他 request，不會阻塞整個 FastAPI process。
        """

        # 先把 Domain query/principal 組成 port 所需 DTO，再等待外部 KIS 回覆。
        search_result = await self._knowledge_search.search(
            KnowledgeSearchCriteria(
                query=query.text,
                limit=self._policy.candidate_limit_for(query.limit),
                principal=principal,
                language=query.language,
                score_threshold=self._policy.score_threshold,
            )
        )

        selected = self._select_candidates(search_result.candidates, result_limit=query.limit)
        # generator expression 逐筆過濾；tuple 使結果不可被下游意外修改。
        llm_context = tuple(item for item in selected if item.governance.external_llm_allowed)
        restricted_evidence = tuple(
            item for item in selected if not item.governance.external_llm_allowed
        )

        # 預設採 fail-closed：沒有可用證據時不能假裝找到答案。
        status = RetrievalStatus.INSUFFICIENT_EVIDENCE
        if llm_context:
            status = RetrievalStatus.EVIDENCE_FOUND
        elif restricted_evidence:
            status = RetrievalStatus.EXTERNAL_GENERATION_BLOCKED

        return RetrievalResult(
            status=status,
            llm_context=llm_context,
            restricted_evidence=restricted_evidence,
            candidate_count=len(search_result.candidates),
            model_id=search_result.model_id,
            model_revision=search_result.model_revision,
        )

    def _select_candidates(
        self,
        candidates: tuple[CandidateChunk, ...],
        *,
        result_limit: int,
    ) -> tuple[Evidence, ...]:
        """以穩定排序、去重、文件上限與 token budget 選取候選。"""

        # ``key=lambda ...`` 指定排序依據；reverse=True 代表分數由高到低。
        ranked = sorted(candidates, key=lambda candidate: candidate.score, reverse=True)
        # Identical text can legitimately exist in documents with different
        # external-processing policies. Keep one best candidate per governance
        # class so a higher-scoring restricted copy cannot hide an allowed copy.
        seen_content: set[tuple[str, bool]] = set()
        # Counter 是帶有預設值 0 的計數字典，適合追蹤每份文件已選幾個 chunk。
        per_document_count: Counter[UUID] = Counter()
        remaining_context_tokens = self._policy.max_context_tokens
        selected: list[Evidence] = []

        for candidate in ranked:
            # ``continue`` 會略過本輪候選；``break`` 則直接結束整個迴圈。
            if len(selected) >= result_limit:
                break
            if (
                self._policy.score_threshold is not None
                and candidate.score < self._policy.score_threshold
            ):
                continue
            content_identity = (
                candidate.content,
                candidate.external_llm_allowed,
            )
            if content_identity in seen_content:
                continue
            if per_document_count[candidate.document_id] >= self._policy.max_chunks_per_document:
                continue
            if candidate.external_llm_allowed and candidate.token_count > remaining_context_tokens:
                continue

            evidence = Evidence.from_candidate(candidate, rank=len(selected) + 1)
            selected.append(evidence)
            seen_content.add(content_identity)
            per_document_count[candidate.document_id] += 1
            if candidate.external_llm_allowed:
                remaining_context_tokens -= candidate.token_count

        return tuple(selected)

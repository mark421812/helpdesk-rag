"""Application layer 的 ports（介面）。

``Protocol`` 採 structural typing：實作類別不必繼承它，只要提供相同方法就符合介面。
因此 use case 依賴抽象 port，測試可換成 fake，正式環境再注入 HTTP adapter。
"""

from __future__ import annotations

from typing import Protocol

from retrieval_service.application.dtos import (
    IdentityInput,
    KnowledgeSearchCriteria,
    KnowledgeSearchResult,
)
from retrieval_service.domain.entities import Evidence, PrincipalContext, RetrievalQuery


class KnowledgeSearchPort(Protocol):
    """查詢知識候選的 outbound port，由 KIS HTTP client 實作。"""

    async def search(self, criteria: KnowledgeSearchCriteria) -> KnowledgeSearchResult: ...

    async def is_ready(self) -> bool: ...


class IdentityProviderPort(Protocol):
    """把未驗證輸入解析成可信 PrincipalContext 的 port。"""

    async def resolve(self, identity: IdentityInput) -> PrincipalContext: ...


class ContextAttestorPort(Protocol):
    """替 Retrieval 核准的問題與 llm_context 簽發不可竄改的短效封條。"""

    def issue(self, query: RetrievalQuery, evidence: tuple[Evidence, ...]) -> str: ...

    def is_ready(self) -> bool: ...

"""Composition Root：集中建立並組裝 Retrieval 的正式物件圖。

Clean Architecture 讓內層不知道外層實作；因此需要在最外層把 HTTP adapter、identity
adapter、policy 與 use case 接起來。這種明確組裝就是手動 Dependency Injection。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from retrieval_service.application.dtos import RetrievalPolicy
from retrieval_service.application.ports import (
    ContextAttestorPort,
    IdentityProviderPort,
    KnowledgeSearchPort,
)
from retrieval_service.application.use_cases import RetrieveEvidence
from retrieval_service.infrastructure.attestation.ed25519 import Ed25519ContextAttestor
from retrieval_service.infrastructure.config import Settings
from retrieval_service.infrastructure.identity.development import (
    DevelopmentHeaderIdentityProvider,
)
from retrieval_service.infrastructure.knowledge_index.http_client import (
    KnowledgeIndexHttpClient,
)


@dataclass(slots=True)
class Runtime:
    """FastAPI lifespan 期間共用的依賴容器。"""

    knowledge_search: KnowledgeSearchPort
    identity_provider: IdentityProviderPort
    context_attestor: ContextAttestorPort
    retrieve_evidence: RetrieveEvidence
    close_callback: Callable[[], Awaitable[None]] | None = None

    async def readiness_checks(self) -> dict[str, bool]:
        """執行不改變資料的 dependency readiness checks。"""

        return {
            "identity_provider": True,
            "knowledge_index": await self.knowledge_search.is_ready(),
            "context_attestor": self.context_attestor.is_ready(),
        }

    async def close(self) -> None:
        """釋放 Runtime 擁有的 HTTP connection pool。"""

        if self.close_callback is not None:
            await self.close_callback()


def build_runtime(settings: Settings) -> Runtime:
    """依設定建立 production-shaped Runtime；未實作的身分模式會 fail closed。"""

    # 先拒絕無法驗證的 mode，再建立任何對外連線，避免服務帶著假安全設定啟動。
    if settings.identity_mode != "development_headers":
        raise RuntimeError(
            "The entra_jwt identity adapter is reserved but not implemented; "
            "retrieval-service refuses to trust an unverified production principal"
        )

    knowledge_search = KnowledgeIndexHttpClient(
        settings.knowledge_index_url,
        api_key=settings.knowledge_index_api_key,
        connect_timeout_seconds=settings.knowledge_index_connect_timeout_seconds,
        read_timeout_seconds=settings.knowledge_index_read_timeout_seconds,
        total_timeout_seconds=settings.knowledge_index_total_timeout_seconds,
        readiness_timeout_seconds=settings.knowledge_index_readiness_timeout_seconds,
        max_response_body_bytes=settings.knowledge_index_max_response_body_bytes,
        retry_attempts=settings.knowledge_index_retry_attempts,
        retry_backoff_seconds=settings.knowledge_index_retry_backoff_seconds,
        retry_max_backoff_seconds=settings.knowledge_index_retry_max_backoff_seconds,
    )
    identity_provider = DevelopmentHeaderIdentityProvider()
    context_attestor = Ed25519ContextAttestor(
        private_key_path=settings.attestation_private_key_path,
        key_id=settings.attestation_key_id,
        ttl_seconds=settings.attestation_ttl_seconds,
        issuer=settings.attestation_issuer,
        audience=settings.attestation_audience,
        policy_version=settings.attestation_policy_version,
    )
    retrieve_evidence = RetrieveEvidence(
        knowledge_search,
        RetrievalPolicy(
            candidate_multiplier=settings.candidate_multiplier,
            max_candidate_limit=settings.max_candidate_limit,
            max_context_tokens=settings.max_context_tokens,
            max_chunks_per_document=settings.max_chunks_per_document,
            score_threshold=settings.score_threshold,
        ),
    )
    return Runtime(
        knowledge_search=knowledge_search,
        identity_provider=identity_provider,
        context_attestor=context_attestor,
        retrieve_evidence=retrieve_evidence,
        close_callback=knowledge_search.close,
    )

from __future__ import annotations

import os

import pytest

from retrieval_service.application.dtos import RetrievalPolicy
from retrieval_service.application.use_cases import RetrieveEvidence
from retrieval_service.domain.entities import PrincipalContext, RetrievalQuery
from retrieval_service.domain.enums import RetrievalStatus
from retrieval_service.infrastructure.knowledge_index.http_client import (
    KnowledgeIndexHttpClient,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("KNOWLEDGE_INDEX_INTEGRATION_URL"),
        reason="Set KNOWLEDGE_INDEX_INTEGRATION_URL after seeding the KIS smoke fixture",
    ),
]


@pytest.mark.asyncio
async def test_seeded_kis_fixture_is_retrieved_but_never_externalized() -> None:
    client = KnowledgeIndexHttpClient(
        os.environ["KNOWLEDGE_INDEX_INTEGRATION_URL"],
        api_key=os.getenv("KNOWLEDGE_INDEX_API_KEY") or None,
        retry_backoff_seconds=0,
        retry_max_backoff_seconds=0,
    )
    use_case = RetrieveEvidence(
        client,
        RetrievalPolicy(
            candidate_multiplier=3,
            max_candidate_limit=50,
            max_context_tokens=3_000,
            max_chunks_per_document=3,
        ),
    )
    try:
        result = await use_case.execute(
            RetrievalQuery(text="smoke-rag-2026", limit=8, language="zh-TW"),
            PrincipalContext(
                user_id="retrieval-integration-user",
                group_ids=("all-employees",),
            ),
        )
    finally:
        await client.close()

    assert result.status is RetrievalStatus.EXTERNAL_GENERATION_BLOCKED
    assert result.llm_context == ()
    assert result.restricted_evidence
    assert all(not item.governance.external_llm_allowed for item in result.restricted_evidence)
    assert all(not hasattr(item.citation, "source_path") for item in result.all_evidence)
    assert all(not hasattr(item.citation, "source_url") for item in result.all_evidence)
